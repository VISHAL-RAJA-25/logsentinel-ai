import json
from pathlib import Path

import git
import pytest

from app.graph.pipeline import investigate
from app.services.sandbox import SandboxUnavailable, load_scenario
from app.services.storage import Store

ROOT = Path(__file__).resolve().parents[2]
DATASET = "001"
SCENARIO = load_scenario(DATASET)
BRANCH = SCENARIO["branch"]
LOG = ROOT / "datasets" / DATASET / "app.log"
USER_FILE = "demo_app/shop/user_service.py"


def _branch_exists() -> bool:
    try:
        git.Repo(ROOT).commit(BRANCH)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _branch_exists(), reason=f"branch {BRANCH} not available locally")

ANALYSIS = json.dumps({
    "summary": "Login crashes for unknown emails.",
    "hypotheses": [{
        "cause": "user is None for an unknown email", "confidence": 90,
        "evidence": ["`user.password_hash` is read without a None check"],
        "suggested_fix": "Add a None check", "risk_level": "low", "files_involved": [USER_FILE],
    }],
})
GOOD = {"explanation": "Return early when the user does not exist.", "edits": [{
    "file": USER_FILE,
    "old": "    if user.password_hash != hash_password(password):",
    "new": "    if user is None:\n        return None\n    if user.password_hash != hash_password(password):",
}]}
USELESS = {"explanation": "Rename a log message.", "edits": [{
    "file": USER_FILE,
    "old": '    logger.info("Login success for user id=%s", user.id)',
    "new": '    logger.info("Login OK for user id=%s", user.id)',
}]}


def make_llm(*patches):
    """Answers analysis requests with ANALYSIS and patch requests with the given patches in order."""
    patch_prompts = []

    def llm(messages):
        if "Site Reliability Engineer" in messages[0]["content"]:
            return ANALYSIS
        patch_prompts.append(messages[-1]["content"])
        return json.dumps(patches[min(len(patch_prompts), len(patches)) - 1])

    llm.patch_prompts = patch_prompts
    return llm


def run(tmp_path, llm):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    state = investigate(LOG, ref=BRANCH, llm=llm, db_url=url, with_patch=True,
                        replay_requests=SCENARIO["requests"], use_llm_for_classification=False)
    return state, Store(url)


@pytest.fixture(autouse=True)
def local_sandbox(monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "local")


def test_validated_patch_ends_up_in_the_report_and_database(tmp_path):
    state, store = run(tmp_path, make_llm(GOOD))
    assert state["validation"].passed and state["patch_attempt"] == 1
    assert "VALIDATED in the sandbox" in state["report"] and "```diff" in state["report"]
    saved = store.get(state["id"])
    assert saved["patch_status"] == "validated" and "user is None" in saved["patch_diff"]
    steps = [t["step"] for t in saved["timeline"]]
    assert steps == ["parse", "classify", "explore", "analyze", "patch", "validate", "report"]


def test_rejected_patch_is_corrected_on_the_next_attempt(tmp_path):
    llm = make_llm(USELESS, GOOD)
    state, _ = run(tmp_path, llm)
    assert state["patch_attempt"] == 2 and state["validation"].passed
    assert [h["passed"] for h in state["patch_history"]] == [False, True]
    assert "YOUR PREVIOUS ATTEMPT FAILED" in llm.patch_prompts[1]


def test_gives_up_after_three_rejected_patches(tmp_path):
    state, store = run(tmp_path, make_llm(USELESS))
    assert state["patch_attempt"] == 3 and not state["validation"].passed
    assert state["status"] == "completed" and "REJECTED by the sandbox" in state["report"]
    assert store.get(state["id"])["patch_status"] == "rejected"


def test_unavailable_sandbox_degrades_gracefully(tmp_path, monkeypatch):
    def no_docker(*args, **kwargs):
        raise SandboxUnavailable("no docker")

    monkeypatch.setattr("app.graph.pipeline.validate_patch", no_docker)
    state, store = run(tmp_path, make_llm(GOOD))
    assert state["status"] == "completed" and state["validation"] is None
    assert "NOT VALIDATED" in state["report"]
    assert store.get(state["id"])["patch_status"] == "unvalidated"
