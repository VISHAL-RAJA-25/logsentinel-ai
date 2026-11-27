"""Evaluation: how well does LogSentinel AI do on the 15 known incidents?

Each incident is compared with the answer key in scenarios/scenarios.json:
  * diagnosis - is the real root cause the top hypothesis (top-1) or one of the three (top-3)?
                Judged by an LLM against the ground truth, with a keyword check as a fallback.
  * category  - did the classifier pick the right kind of problem?
  * patch     - validated / needs_review / rejected / no_patch (sandbox + review verdict)
  * time      - seconds per investigation (includes any rate-limit waiting)

    python -m app.evaluation            score the latest stored run of each incident
    python -m app.evaluation --rerun    run all 15 again (needs Docker), then score

Results go to results/evaluation.json and results/EVALUATION.md (paste the table into the README).
"""
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from app.services.storage import Store

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"

JUDGE_PROMPT = """You grade an automated incident investigation against a known answer.
Decide which candidate diagnosis, if any, identifies the SAME root cause as the ground truth.
A candidate matches only if it names the same defect (same mechanism in the same place), not merely the same symptom
or a vague area. Reply with JSON only, no markdown fences:
{"matching_rank": 1 or 2 or 3 or null, "reason": "<one sentence>"}"""


def load_scenarios() -> list[dict]:
    return json.loads((ROOT / "scenarios" / "scenarios.json").read_text())


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


def keyword_hits(hypothesis: dict, keywords: list[str]) -> int:
    text = " ".join([hypothesis.get("cause", ""), hypothesis.get("suggested_fix", ""),
                     *hypothesis.get("evidence", [])]).lower()
    return sum(1 for keyword in keywords if keyword.lower() in text)


def judge(truth: str, hypotheses: list[dict], llm: Callable) -> tuple[Optional[int], str, bool]:
    """Returns (rank of the matching hypothesis or None, reason, whether the judge worked)."""
    candidates = "\n".join(f"{i}. {h['cause']}" for i, h in enumerate(hypotheses, 1))
    user = f"GROUND TRUTH ROOT CAUSE:\n{truth}\n\nCANDIDATE DIAGNOSES:\n{candidates}"
    try:
        data = _extract_json(llm([{"role": "system", "content": JUDGE_PROMPT},
                                  {"role": "user", "content": user}]))
        rank = data.get("matching_rank")
        rank = int(rank) if rank is not None else None
        if rank is not None and not 1 <= rank <= len(hypotheses):
            rank = None
        return rank, str(data.get("reason", ""))[:200], True
    except Exception as exc:  # rate limit, bad JSON ...
        return None, f"judge unavailable ({type(exc).__name__})", False


def pick_runs(rows: list[dict], scenarios: list[dict], min_id: int = 0,
              prefer_patch: bool = True) -> dict[str, Optional[dict]]:
    """Newest stored run per incident (rows must be newest first)."""
    picked = {}
    for sc in scenarios:
        same = [r for r in rows if r["ref"] == sc["branch"] and r["id"] >= min_id]
        with_patch = [r for r in same if r.get("patch_status") not in (None, "none")]
        picked[sc["id"]] = ((with_patch if prefer_patch and with_patch else same) or [None])[0]
    return picked


def score_run(scenario: dict, run: Optional[dict], llm: Callable) -> dict:
    truth = scenario["ground_truth"]
    row = {"id": scenario["id"], "title": scenario["title"], "branch": scenario["branch"],
           "expected_category": truth["category"], "expected_cause": truth["root_cause"],
           "run_id": None, "status": "not run", "category": None, "category_ok": False,
           "top1": False, "top3": False, "scored_by": "n/a", "judge_reason": "",
           "keyword_hits": 0, "top_confidence": None, "patch_status": None, "patch_attempts": 0,
           "duration_s": None, "patch_warnings": []}
    if run is None:
        return row
    hypotheses = (run.get("analysis") or {}).get("hypotheses", [])
    keywords = truth["correct_fix_keywords"]
    row.update(run_id=run["id"], status=run["status"], category=run.get("category"),
               category_ok=run.get("category") == truth["category"],
               patch_status=run.get("patch_status"), patch_attempts=run.get("patch_attempts") or 0,
               duration_s=run.get("duration_s"),
               top_confidence=hypotheses[0]["confidence"] if hypotheses else None,
               patch_warnings=((run.get("validation") or {}).get("final") or {}).get("warnings", []))
    if not hypotheses:
        row["judge_reason"] = "no diagnosis was produced"
        return row
    row["keyword_hits"] = keyword_hits(hypotheses[0], keywords)
    rank, reason, judged = judge(truth["root_cause"], hypotheses, llm)
    if judged:
        row.update(top1=rank == 1, top3=rank is not None, scored_by="llm judge", judge_reason=reason)
    else:  # fall back to keywords: at least 2 of the 3 expected keywords
        row.update(top1=row["keyword_hits"] >= 2,
                   top3=any(keyword_hits(h, keywords) >= 2 for h in hypotheses),
                   scored_by="keywords", judge_reason=reason)
    return row


def summarize(rows: list[dict]) -> dict:
    ran = [r for r in rows if r["status"] != "not run"]
    durations = sorted(r["duration_s"] for r in ran if r["duration_s"] is not None)
    patches = Counter(r["patch_status"] for r in ran)
    right = [r["top_confidence"] for r in ran if r["top1"] and r["top_confidence"] is not None]
    wrong = [r["top_confidence"] for r in ran if not r["top1"] and r["top_confidence"] is not None]
    return {
        "incidents": len(rows), "ran": len(ran),
        "diagnosed_top1": sum(r["top1"] for r in ran),
        "diagnosed_top3": sum(r["top3"] for r in ran),
        "category_correct": sum(r["category_ok"] for r in ran),
        "patch_counts": dict(patches),
        "validated": patches.get("validated", 0),
        "median_s": round(statistics.median(durations), 1) if durations else None,
        "mean_s": round(statistics.mean(durations), 1) if durations else None,
        "p90_s": round(durations[int(0.9 * (len(durations) - 1))], 1) if durations else None,
        "avg_confidence_when_right": round(statistics.mean(right)) if right else None,
        "avg_confidence_when_wrong": round(statistics.mean(wrong)) if wrong else None,
    }


def render_markdown(rows: list[dict], summary: dict, rerun: bool) -> str:
    s, n = summary, summary["ran"]
    needs_review = s["patch_counts"].get("needs_review", 0)
    right, wrong = s["avg_confidence_when_right"], s["avg_confidence_when_wrong"]
    confidence_note = (f"The model's stated confidence is not calibrated: it averaged "
                       f"{'n/a' if right is None else str(right) + '%'} when right and "
                       f"{'n/a (there were no wrong diagnoses)' if wrong is None else str(wrong) + '%'} when wrong.")
    lines = [
        "# Evaluation results", "",
        f"Evaluated on **{s['incidents']} injected failure scenarios** of a FastAPI demo app "
        f"({'fresh run of all incidents' if rerun else 'latest stored run of each incident'}).", "",
        "| Metric | Result |", "|---|---|",
        f"| Correct root cause ranked #1 | {s['diagnosed_top1']} / {n} |",
        f"| Correct root cause in the top 3 | {s['diagnosed_top3']} / {n} |",
        f"| Problem category classified correctly | {s['category_correct']} / {n} |",
        f"| Patch validated in the sandbox, no review warnings | {s['validated']} / {n} |",
        f"| Patch passed but flagged for human review | {needs_review} / {n} |",
        f"| Patch rejected, none produced, or sandbox unavailable | {n - s['validated'] - needs_review} / {n} |",
        f"| Time per investigation (median / 90th percentile) | {s['median_s']} s / {s['p90_s']} s |",
        "", "## Per incident", "",
        "| ID | Incident | Diagnosis | Category | Patch | Tries | Time |", "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r["status"] == "not run":
            diagnosis = "not run"
        else:
            diagnosis = "top-1" if r["top1"] else "top-3 only" if r["top3"] else "missed"
        category = "ok" if r["category_ok"] else f"got {r['category']}"
        time_s = f"{r['duration_s']} s" if r["duration_s"] is not None else "-"
        lines.append(f"| {r['id']} | {r['title']} | {diagnosis} | {category} | {r['patch_status']} | "
                     f"{r['patch_attempts']} | {time_s} |")
    lines += [
        "", "## Honest limits", "",
        "- These are **development scenarios**: the classifier rules and prompts were tuned while looking at them, "
        "so the numbers are optimistic. Unseen bugs are the real test.",
        "- In every scenario the newest commit is the bug, which makes diagnosis easier than in most real incidents.",
        "- **Validated** means: the bug reproduced in a no-network container, the app's tests passed, the failing "
        "request stopped crashing, rule-based checks found no masking tricks, and an AI reviewer agreed. It is not "
        "proof of correctness, so a human reviews every patch and it opens only as a draft pull request.",
        "- Some incidents (a missing secret, an uninstalled package, a service that is down) cannot be fixed by "
        "editing code. The system flags them for a human instead of faking a fix.",
        "- Diagnosis was graded by an LLM judge against the answer key (keyword matching as a fallback), "
        "so the grader itself can be wrong.",
        f"- {confidence_note}",
        "- Times include waiting for free-tier LLM rate limits, so the median is the fairer figure.", "",
    ]
    return "\n".join(lines)


def run_all(scenarios: list[dict]) -> None:
    from app.graph.pipeline import investigate  # imported here so scoring works without the full pipeline

    for sc in scenarios:
        print(f"running incident {sc['id']}: {sc['title']} ...", flush=True)
        investigate(ROOT / "datasets" / sc["id"] / "app.log", ref=sc["branch"], with_patch=True,
                    replay_requests=sc["requests"])
        time.sleep(8)  # stay under free-tier rate limits


def main(argv: list[str]) -> None:
    from app.agents.root_cause import default_llm

    rerun = "--rerun" in argv
    scenarios, store = load_scenarios(), Store()
    min_id = 0
    if rerun:
        from app.services.sandbox import SandboxUnavailable, get_client

        try:
            get_client()
        except SandboxUnavailable as exc:
            sys.exit(f"{exc}\nStart Docker Desktop first, then run this again.")
        newest = store.list(limit=1)
        min_id = newest[0]["id"] + 1 if newest else 1
        run_all(scenarios)
    runs = pick_runs(store.list(limit=2000), scenarios, min_id=min_id, prefer_patch=not rerun)
    scored = []
    for sc in scenarios:
        print(f"scoring incident {sc['id']} ...", flush=True)
        scored.append(score_run(sc, runs[sc["id"]], default_llm))
        time.sleep(3)
    summary = summarize(scored)
    RESULTS.mkdir(exist_ok=True)
    report = render_markdown(scored, summary, rerun)
    (RESULTS / "evaluation.json").write_text(json.dumps({"summary": summary, "incidents": scored}, indent=2))
    (RESULTS / "EVALUATION.md").write_text(report)
    print("\n" + report)
    print("Saved results/EVALUATION.md and results/evaluation.json")


if __name__ == "__main__":
    main(sys.argv[1:])
