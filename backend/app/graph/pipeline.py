"""LangGraph pipeline:

  parse -> classify -> explore -> analyze -> (expand) -> [patch -> validate -> (retry patch)] -> report

All agents read and write one shared state. Two conditional loops:
  * weak or ungrounded diagnosis  -> gather more context, analyze once more
  * patch rejected by the sandbox -> feed the failure back, generate a new patch (max 3)
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.classifier import Classification, classify
from app.agents.log_parser import parse_log_file
from app.agents.patch_generator import PatchError, generate_patch
from app.agents.repo_explorer import explore, format_context
from app.agents.root_cause import AnalysisError, analyze, default_llm
from app.graph.report import build_report
from app.models.analysis import Analysis
from app.models.incident import Incident
from app.models.patch import PatchProposal, ValidationResult
from app.services.sandbox import describe_failure, load_scenario, validate_patch
from app.services.storage import Store

ROOT = Path(__file__).resolve().parents[3]
CONFIDENCE_THRESHOLD = 70
MAX_ATTEMPTS = 2        # first analysis + at most one retry with more context
MAX_PATCH_ATTEMPTS = 3  # first patch + at most two corrections


class InvestigationState(TypedDict, total=False):
    log_path: str
    ref: str
    repo_path: str
    replay_requests: Optional[list]
    incident: Optional[Incident]
    incident_count: int
    classification: Classification
    context_text: str
    analysis: Analysis
    attempt: int
    patch_attempt: int
    patch: Optional[PatchProposal]
    patch_error: Optional[str]
    patch_feedback: Optional[str]
    patch_history: list[dict]
    validation: Optional[ValidationResult]
    sandbox_error: Optional[str]
    status: str  # running | completed | no_incident | failed
    error: str
    report: str
    timeline: list[dict]


def _log(state: dict, step: str, detail: str) -> list[dict]:
    entry = {"step": step, "at": datetime.now().strftime("%H:%M:%S"), "detail": detail}
    return list(state.get("timeline", [])) + [entry]


def needs_more_context(analysis: Analysis, threshold: int = CONFIDENCE_THRESHOLD) -> bool:
    top = analysis.hypotheses[0]
    weak = top.confidence < threshold
    ungrounded = top.quotes_total > 0 and top.quotes_verified == 0
    return weak or ungrounded


def build_graph(llm: Callable = default_llm, max_commits: int = 5,
                use_llm_for_classification: bool = True, with_patch: bool = False):
    def parse_node(state):
        incidents = parse_log_file(state["log_path"])
        if not incidents:
            return {"status": "no_incident",
                    "timeline": _log(state, "parse", "no error with a traceback found in the log")}
        incident = max(incidents, key=lambda i: (i.frequency, i.last_seen))
        detail = (f"{len(incidents)} incident(s) found; investigating {incident.error_type} "
                  f"in {incident.function}() (seen {incident.frequency}x)")
        return {"incident": incident, "incident_count": len(incidents),
                "timeline": _log(state, "parse", detail)}

    def classify_node(state):
        result = classify(state["incident"], use_llm=use_llm_for_classification)
        return {"classification": result,
                "timeline": _log(state, "classify", f"{result.category or 'unclassified'} ({result.source}: {result.reason})")}

    def explore_node(state):
        ctx = explore(state["incident"], ref=state["ref"], repo_path=state["repo_path"],
                      max_commits=max_commits)
        latest = f"'{ctx.recent_commits[0].message}'" if ctx.recent_commits else "none"
        return {"context_text": format_context(ctx),
                "timeline": _log(state, "explore", f"{len(ctx.recent_commits)} recent commits; latest {latest}")}

    def analyze_node(state):
        try:
            analysis = analyze(state["incident"], state["classification"],
                               state["context_text"], llm=llm)
        except AnalysisError as exc:
            return {"status": "failed", "error": str(exc),
                    "timeline": _log(state, "analyze", f"failed: {str(exc)[:120]}")}
        top = analysis.hypotheses[0]
        return {"analysis": analysis, "attempt": state.get("attempt", 0) + 1,
                "timeline": _log(state, "analyze", f"top cause at {top.confidence}%: {top.cause[:90]}")}

    def expand_node(state):
        ctx = explore(state["incident"], ref=state["ref"], repo_path=state["repo_path"],
                      max_commits=max_commits * 2)
        return {"context_text": format_context(ctx, diff_commits=4),
                "timeline": _log(state, "expand_context",
                                 f"weak or ungrounded answer; retrying with {len(ctx.recent_commits)} commits and 4 diffs")}

    def patch_node(state):
        top = state["analysis"].hypotheses[0]
        n = state.get("patch_attempt", 0) + 1
        try:
            proposal = generate_patch(state["incident"], top, state["repo_path"], state["ref"],
                                      llm=llm, feedback=state.get("patch_feedback"))
        except PatchError as exc:
            return {"patch": None, "patch_error": str(exc), "patch_attempt": n,
                    "timeline": _log(state, "patch", f"attempt {n}: no usable patch ({str(exc)[:100]})")}
        files = ", ".join(sorted({e.file for e in proposal.edits}))
        return {"patch": proposal, "patch_error": None, "patch_attempt": n,
                "timeline": _log(state, "patch", f"attempt {n}: edits {files}")}

    def validate_node(state):
        patch, n = state["patch"], state["patch_attempt"]
        try:
            result = validate_patch(patch.edits, state["repo_path"], state["ref"],
                                    requests=state.get("replay_requests"))
        except Exception as exc:  # Docker not running, image missing, bad ref ...
            message = f"{type(exc).__name__}: {str(exc)[:200]}"
            return {"validation": None, "sandbox_error": message,
                    "timeline": _log(state, "validate", f"could not run the sandbox: {message}")}
        history = list(state.get("patch_history", [])) + [
            {"attempt": n, "diff": patch.diff, "passed": result.passed, "reason": result.reason}]
        verdict = "PASSED" if result.passed else f"rejected: {result.reason}"
        return {"validation": result, "sandbox_error": None, "patch_history": history,
                "patch_feedback": None if result.passed else describe_failure(patch.diff, result),
                "timeline": _log(state, "validate", f"attempt {n} {verdict}")}

    def report_node(state):
        timeline = _log(state, "report", "report written")
        report = build_report({**state, "timeline": timeline})
        return {"report": report, "status": "completed", "timeline": timeline}

    def route_after_parse(state):
        return "end" if state.get("status") == "no_incident" else "continue"

    def route_after_analyze(state):
        if state.get("status") == "failed":
            return "end"
        if state.get("attempt", 0) < MAX_ATTEMPTS and needs_more_context(state["analysis"]):
            return "expand"
        return "next"

    def route_after_patch(state):
        return "validate" if state.get("patch") else "report"

    def route_after_validate(state):
        result = state.get("validation")
        if result is None or result.passed:
            return "report"
        return "retry" if state.get("patch_attempt", 0) < MAX_PATCH_ATTEMPTS else "report"

    graph = StateGraph(InvestigationState)
    graph.add_node("parse", parse_node)
    graph.add_node("classify", classify_node)
    graph.add_node("explore", explore_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("expand_context", expand_node)
    graph.add_node("report", report_node)

    graph.add_edge(START, "parse")
    graph.add_conditional_edges("parse", route_after_parse, {"continue": "classify", "end": END})
    graph.add_edge("classify", "explore")
    graph.add_edge("explore", "analyze")
    graph.add_edge("expand_context", "analyze")
    graph.add_edge("report", END)

    if with_patch:
        graph.add_node("patch", patch_node)
        graph.add_node("validate", validate_node)
        graph.add_conditional_edges("analyze", route_after_analyze,
                                    {"expand": "expand_context", "next": "patch", "end": END})
        graph.add_conditional_edges("patch", route_after_patch, {"validate": "validate", "report": "report"})
        graph.add_conditional_edges("validate", route_after_validate, {"retry": "patch", "report": "report"})
    else:
        graph.add_conditional_edges("analyze", route_after_analyze,
                                    {"expand": "expand_context", "next": "report", "end": END})
    return graph.compile()


def investigate(log_path, ref: str = "HEAD", repo_path=ROOT, llm: Callable = default_llm,
                save: bool = True, db_url: Optional[str] = None,
                use_llm_for_classification: bool = True, with_patch: bool = False,
                replay_requests: Optional[list] = None) -> dict:
    app = build_graph(llm=llm, use_llm_for_classification=use_llm_for_classification,
                      with_patch=with_patch)
    initial = {"log_path": str(log_path), "ref": ref, "repo_path": str(repo_path),
               "replay_requests": replay_requests, "attempt": 0, "patch_attempt": 0,
               "status": "running", "timeline": []}
    started = time.perf_counter()
    try:
        state = app.invoke(initial)
    except Exception as exc:  # e.g. the git ref does not exist
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        state = {**initial, "status": "failed", "error": message,
                 "timeline": _log(initial, "error", message)}
    state["duration_s"] = round(time.perf_counter() - started, 2)
    if save:
        state["id"] = Store(db_url).save(state)
    return state


def _print_list() -> None:
    for r in Store().list():
        print(f"#{r['id']:<4} {r['status']:<10} {str(r['error_type']):<20} "
              f"{str(r['top_confidence']) + '%':<5} patch: {str(r['patch_status']):<11} "
              f"{r['duration_s']}s  {r['ref']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    positional = [a for a in args if not a.startswith("--")]
    if "--list" in flags:
        _print_list()
        sys.exit()
    if not positional:
        sys.exit("Usage: python -m app.graph.pipeline <dataset id> [--patch]\n"
                 "   or: python -m app.graph.pipeline <log path> <git ref> [--patch]\n"
                 "   or: python -m app.graph.pipeline --list")
    replay = None
    if len(positional) == 1 and positional[0].isdigit():
        scenario = load_scenario(positional[0])
        log_path = ROOT / "datasets" / positional[0] / "app.log"
        ref, replay = scenario["branch"], scenario["requests"]
    else:
        log_path, ref = Path(positional[0]), (positional[1] if len(positional) > 1 else "HEAD")
    result = investigate(log_path, ref=ref, with_patch="--patch" in flags, replay_requests=replay)
    print(result.get("report") or f"Run ended with status '{result['status']}': {result.get('error', '')}")
    print(f"\nSaved as investigation #{result.get('id')} | status: {result['status']} | "
          f"{result['duration_s']}s")
