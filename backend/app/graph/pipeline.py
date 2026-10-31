"""LangGraph pipeline: parse -> classify -> explore -> analyze -> (expand) -> report.

All agents read and write one shared state. After `analyze`, a conditional edge
decides: weak or ungrounded answer -> gather more context and analyze once more,
otherwise -> write the report.
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
from app.agents.repo_explorer import explore, format_context
from app.agents.root_cause import AnalysisError, analyze, default_llm
from app.graph.report import build_report
from app.models.analysis import Analysis
from app.models.incident import Incident
from app.services.storage import Store

ROOT = Path(__file__).resolve().parents[3]
CONFIDENCE_THRESHOLD = 70
MAX_ATTEMPTS = 2  # first analysis + at most one retry with more context


class InvestigationState(TypedDict, total=False):
    log_path: str
    ref: str
    repo_path: str
    incident: Optional[Incident]
    incident_count: int
    classification: Classification
    context_text: str
    analysis: Analysis
    attempt: int
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
                use_llm_for_classification: bool = True):
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
        return "report"

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
    graph.add_conditional_edges("analyze", route_after_analyze,
                                {"expand": "expand_context", "report": "report", "end": END})
    graph.add_edge("expand_context", "analyze")
    graph.add_edge("report", END)
    return graph.compile()


def investigate(log_path, ref: str = "HEAD", repo_path=ROOT, llm: Callable = default_llm,
                save: bool = True, db_url: Optional[str] = None,
                use_llm_for_classification: bool = True) -> dict:
    app = build_graph(llm=llm, use_llm_for_classification=use_llm_for_classification)
    initial = {"log_path": str(log_path), "ref": ref, "repo_path": str(repo_path),
               "attempt": 0, "status": "running", "timeline": []}
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
        print(f"#{r['id']:<4} {r['status']:<12} {str(r['error_type']):<22} "
              f"{str(r['top_confidence']) + '%':<6} {r['duration_s']}s  {r['ref']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--list"]:
        _print_list()
        sys.exit()
    if not args:
        sys.exit("Usage: python -m app.graph.pipeline <dataset id>\n"
                 "   or: python -m app.graph.pipeline <log path> <git ref>\n"
                 "   or: python -m app.graph.pipeline --list")
    if len(args) == 1 and args[0].isdigit():
        folder = ROOT / "datasets" / args[0]
        log_path = folder / "app.log"
        ref = json.loads((folder / "ground_truth.json").read_text())["branch"]
    else:
        log_path, ref = Path(args[0]), (args[1] if len(args) > 1 else "HEAD")
    result = investigate(log_path, ref=ref)
    print(result.get("report") or f"Run ended with status '{result['status']}': {result.get('error', '')}")
    print(f"\nSaved as investigation #{result.get('id')} | status: {result['status']} | {result['duration_s']}s")
