import json
from pathlib import Path

import git
import pytest

from app.graph.pipeline import investigate
from app.services.storage import Store

ROOT = Path(__file__).resolve().parents[2]
DATASET = "001"
LOG = ROOT / "datasets" / DATASET / "app.log"
BRANCH = json.loads((ROOT / "datasets" / DATASET / "ground_truth.json").read_text())["branch"]


def _branch_exists() -> bool:
    try:
        git.Repo(ROOT).commit(BRANCH)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _branch_exists(), reason=f"branch {BRANCH} not available locally")


def make_llm(confidence=90, quote="`user.password_hash`"):
    reply = json.dumps({
        "summary": "Login crashes for unknown emails.",
        "hypotheses": [{
            "cause": "user is None for an unknown email", "confidence": confidence,
            "evidence": [f"{quote} is read without a None check"],
            "suggested_fix": "Add a None check", "risk_level": "low",
            "files_involved": ["demo_app/shop/user_service.py"],
        }],
    })
    calls = []

    def llm(messages):
        calls.append(1)
        return reply

    llm.calls = calls
    return llm


def run(tmp_path, llm, log=LOG, ref=BRANCH):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    state = investigate(log, ref=ref, llm=llm, db_url=url, use_llm_for_classification=False)
    return state, Store(url)


def test_full_run_writes_report_and_saves_it(tmp_path):
    llm = make_llm(90)
    state, store = run(tmp_path, llm)
    assert state["status"] == "completed"
    assert state["attempt"] == 1 and len(llm.calls) == 1
    assert "AttributeError" in state["report"] and "Probable root causes" in state["report"]
    saved = store.get(state["id"])
    assert saved["status"] == "completed" and saved["top_confidence"] == 90
    assert [t["step"] for t in saved["timeline"]] == ["parse", "classify", "explore", "analyze", "report"]


def test_low_confidence_triggers_exactly_one_retry(tmp_path):
    llm = make_llm(30)
    state, _ = run(tmp_path, llm)
    assert state["status"] == "completed"
    assert state["attempt"] == 2 and len(llm.calls) == 2
    assert "expand_context" in [t["step"] for t in state["timeline"]]


def test_ungrounded_top_cause_triggers_retry(tmp_path):
    llm = make_llm(95, quote="`text that is nowhere in the code`")
    state, _ = run(tmp_path, llm)
    assert len(llm.calls) == 2


def test_log_without_errors_ends_early(tmp_path):
    log = tmp_path / "quiet.log"
    log.write_text("2026-10-24 10:11:52,227 | INFO | shop.user_service | Created user id=1 email=a@test.com\n")
    llm = make_llm()
    state, store = run(tmp_path, llm, log=log)
    assert state["status"] == "no_incident"
    assert llm.calls == []
    assert store.get(state["id"])["status"] == "no_incident"


def test_unparseable_llm_reply_marks_run_failed(tmp_path):
    state, store = run(tmp_path, lambda messages: "garbage")
    assert state["status"] == "failed" and "No valid JSON" in state["error"]
    assert store.get(state["id"])["status"] == "failed"


def test_unknown_git_ref_is_recorded_as_failure(tmp_path):
    state, store = run(tmp_path, make_llm(), ref="no-such-branch")
    assert state["status"] == "failed" and state["error"]
    assert store.get(state["id"])["status"] == "failed"


def test_store_lists_newest_first(tmp_path):
    _, store = run(tmp_path, make_llm())
    run(tmp_path, make_llm())
    ids = [r["id"] for r in store.list()]
    assert ids == sorted(ids, reverse=True) and len(ids) == 2
