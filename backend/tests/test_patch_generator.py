import json
from pathlib import Path

import git
import pytest

from app.agents.log_parser import parse_log_file
from app.agents.patch_generator import PatchError, apply_edits, generate_patch
from app.models.analysis import Hypothesis
from app.models.patch import PatchEdit

ROOT = Path(__file__).resolve().parents[2]
DATASET = "001"
BRANCH = json.loads((ROOT / "datasets" / DATASET / "ground_truth.json").read_text())["branch"]
USER_FILE = "demo_app/shop/user_service.py"


def _branch_exists() -> bool:
    try:
        git.Repo(ROOT).commit(BRANCH)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _branch_exists(), reason=f"branch {BRANCH} not available locally")

INCIDENT = parse_log_file(ROOT / "datasets" / DATASET / "app.log")[0]
HYPOTHESIS = Hypothesis(cause="user is None for an unknown email", confidence=90,
                        evidence=["`user.password_hash`"], suggested_fix="add a None check",
                        risk_level="low", files_involved=[USER_FILE])

GOOD = {"explanation": "Return early when the user does not exist.", "edits": [{
    "file": USER_FILE,
    "old": "    if user.password_hash != hash_password(password):",
    "new": "    if user is None:\n        return None\n    if user.password_hash != hash_password(password):",
}]}


def fake_llm(*replies):
    calls = []

    def llm(messages):
        calls.append([dict(m) for m in messages])
        reply = replies[min(len(calls), len(replies)) - 1]
        return reply if isinstance(reply, str) else json.dumps(reply)

    llm.calls = calls
    return llm


def gen(llm, feedback=None):
    return generate_patch(INCIDENT, HYPOTHESIS, ROOT, BRANCH, llm=llm, feedback=feedback)


def test_valid_patch_produces_a_unified_diff():
    proposal = gen(fake_llm(GOOD))
    assert f"--- a/{USER_FILE}" in proposal.diff and f"+++ b/{USER_FILE}" in proposal.diff
    assert "+    if user is None:" in proposal.diff


def test_text_not_found_is_fed_back_and_retried():
    bad = {"explanation": "x", "edits": [{"file": USER_FILE, "old": "this text does not exist", "new": "y"}]}
    llm = fake_llm(bad, GOOD)
    assert gen(llm).diff
    assert len(llm.calls) == 2
    assert "appears 0 times" in llm.calls[1][-1]["content"]


def test_two_unusable_replies_raise():
    with pytest.raises(PatchError):
        gen(fake_llm("not json", "still not json"))


def test_tests_cannot_be_edited():
    edit = {"explanation": "x", "edits": [{"file": "demo_app/tests/test_api.py", "old": "a", "new": "b"}]}
    with pytest.raises(PatchError) as error:
        gen(fake_llm(edit, edit))
    assert "only allowed" in str(error.value)


def test_old_text_must_match_exactly_once():
    files = {"demo_app/shop/a.py": "x = 1\nx = 1\n"}
    with pytest.raises(PatchError):
        apply_edits(files, [PatchEdit(file="demo_app/shop/a.py", old="x = 1", new="x = 2")])


def test_previous_failure_is_included_in_the_prompt():
    llm = fake_llm(GOOD)
    gen(llm, feedback="tests failed: boom")
    prompt = llm.calls[0][1]["content"]
    assert "YOUR PREVIOUS ATTEMPT FAILED" in prompt and "tests failed: boom" in prompt
