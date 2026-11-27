import json

from app.evaluation import judge, keyword_hits, pick_runs, render_markdown, score_run, summarize

SCENARIO = {
    "id": "001", "branch": "bug/001", "title": "Login crashes",
    "ground_truth": {"category": "Authentication",
                     "root_cause": "authenticate reads user.password_hash without a None check",
                     "correct_fix_keywords": ["None", "null check", "unknown email"]},
}


def hypothesis(cause, confidence=90, fix="add a check"):
    return {"cause": cause, "confidence": confidence, "evidence": ["`x`"], "suggested_fix": fix}


def run_row(**changes):
    row = {"id": 10, "ref": "bug/001", "status": "completed", "category": "Authentication",
           "patch_status": "validated", "patch_attempts": 1, "duration_s": 5.0,
           "analysis": {"hypotheses": [hypothesis("user is None for an unknown email", fix="add a None check"),
                                       hypothesis("something else", 5)]},
           "validation": {"final": {"warnings": []}}}
    row.update(changes)
    return row


def llm_saying(rank):
    return lambda messages: json.dumps({"matching_rank": rank, "reason": "because"})


def broken_llm(messages):
    raise RuntimeError("rate limited")


# ---------- the judge ----------
def test_judge_returns_the_rank_of_the_matching_diagnosis():
    assert judge("truth", [hypothesis("a"), hypothesis("b")], llm_saying(2)) == (2, "because", True)


def test_judge_can_say_nothing_matches():
    assert judge("truth", [hypothesis("a")], llm_saying(None))[0] is None


def test_judge_ignores_an_impossible_rank():
    assert judge("truth", [hypothesis("a")], llm_saying(3))[0] is None


def test_judge_reports_when_it_could_not_run():
    rank, reason, worked = judge("truth", [hypothesis("a")], broken_llm)
    assert rank is None and worked is False and "unavailable" in reason


def test_keyword_hits_counts_expected_words():
    h = hypothesis("user is None", fix="return for the unknown email")
    assert keyword_hits(h, ["None", "null check", "unknown email"]) == 2


# ---------- scoring one incident ----------
def test_top1_when_the_judge_picks_the_first_diagnosis():
    row = score_run(SCENARIO, run_row(), llm_saying(1))
    assert row["top1"] and row["top3"] and row["category_ok"] and row["scored_by"] == "llm judge"


def test_top3_only_when_the_judge_picks_a_later_diagnosis():
    row = score_run(SCENARIO, run_row(), llm_saying(2))
    assert not row["top1"] and row["top3"]


def test_a_wrong_diagnosis_is_missed():
    row = score_run(SCENARIO, run_row(), llm_saying(None))
    assert not row["top1"] and not row["top3"]


def test_keywords_are_used_when_the_judge_is_unavailable():
    row = score_run(SCENARIO, run_row(), broken_llm)
    assert row["scored_by"] == "keywords" and row["top1"]  # 'None' and 'unknown email' appear in the diagnosis


def test_wrong_category_is_detected():
    assert not score_run(SCENARIO, run_row(category="Database"), llm_saying(1))["category_ok"]


def test_a_failed_run_without_diagnosis_scores_as_missed():
    row = score_run(SCENARIO, run_row(analysis=None, status="failed", patch_status="none"), llm_saying(1))
    assert not row["top1"] and row["judge_reason"] == "no diagnosis was produced"


def test_an_incident_that_was_never_run():
    row = score_run(SCENARIO, None, llm_saying(1))
    assert row["status"] == "not run" and not row["top1"]


# ---------- choosing which stored run to score ----------
def test_runs_with_a_patch_step_are_preferred():
    rows = [run_row(id=9, patch_status="none"), run_row(id=8, patch_status="rejected"), run_row(id=7, ref="bug/other")]
    assert pick_runs(rows, [SCENARIO])["001"]["id"] == 8


def test_after_a_fresh_run_only_new_rows_count():
    rows = [run_row(id=20, patch_status="none", status="failed"), run_row(id=5)]
    assert pick_runs(rows, [SCENARIO], min_id=10, prefer_patch=False)["001"]["id"] == 20
    assert pick_runs([run_row(id=5)], [SCENARIO], min_id=10, prefer_patch=False)["001"] is None


# ---------- totals and the report ----------
def scored_rows():
    return [
        score_run(SCENARIO, run_row(duration_s=4.0), llm_saying(1)),
        score_run({**SCENARIO, "id": "002"}, run_row(duration_s=6.0, patch_status="needs_review"), llm_saying(None)),
        score_run({**SCENARIO, "id": "003"}, run_row(duration_s=30.0, patch_status="rejected", category="Database"), llm_saying(2)),
        score_run({**SCENARIO, "id": "004"}, None, llm_saying(1)),
    ]


def test_summary_numbers():
    s = summarize(scored_rows())
    assert (s["incidents"], s["ran"]) == (4, 3)
    assert (s["diagnosed_top1"], s["diagnosed_top3"], s["category_correct"]) == (1, 2, 2)
    assert s["validated"] == 1 and s["patch_counts"]["needs_review"] == 1
    assert s["median_s"] == 6.0 and s["avg_confidence_when_right"] == 90


def test_the_report_contains_the_table_and_the_honest_limits():
    rows = scored_rows()
    report = render_markdown(rows, summarize(rows), rerun=False)
    assert "| Correct root cause ranked #1 | 1 / 3 |" in report
    assert "| 004 | Login crashes | not run |" in report
    assert "## Honest limits" in report and "development scenarios" in report
