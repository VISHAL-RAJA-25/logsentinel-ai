import json
from pathlib import Path

import git
import pytest

from app.models.patch import PatchEdit, RunReport
from app.services.sandbox import judge, load_scenario, validate_patch

ROOT = Path(__file__).resolve().parents[2]
DATASET = "001"
SCENARIO = load_scenario(DATASET)
BRANCH = SCENARIO["branch"]
USER_FILE = "demo_app/shop/user_service.py"


def _branch_exists() -> bool:
    try:
        git.Repo(ROOT).commit(BRANCH)
        return True
    except Exception:
        return False


needs_branch = pytest.mark.skipif(not _branch_exists(), reason=f"branch {BRANCH} not available locally")

GOOD = PatchEdit(
    file=USER_FILE,
    old="    if user.password_hash != hash_password(password):",
    new="    if user is None:\n        return None\n    if user.password_hash != hash_password(password):",
)
USELESS = PatchEdit(  # changes a log message only, the bug stays
    file=USER_FILE,
    old='    logger.info("Login success for user id=%s", user.id)',
    new='    logger.info("Login OK for user id=%s", user.id)',
)
BROKEN = PatchEdit(  # invalid Python
    file=USER_FILE,
    old='    logger.info("Login success for user id=%s", user.id)',
    new='    logger.info(("Login success"',
)


def check(edit):
    return validate_patch([edit], ROOT, BRANCH, requests=SCENARIO["requests"], mode="local")


@needs_branch
def test_good_patch_is_validated():
    result = check(GOOD)
    assert result.passed, result.reason
    assert result.bug_reproduced
    assert result.baseline.replay_statuses == [201, 500]   # the bug really happens before the patch
    assert result.baseline.tests_failed >= 1                # and an existing test catches it
    assert result.patched.tests_failed == 0
    assert result.patched.replay_statuses[-1] == 401        # unknown email now gives a clean 401


@needs_branch
def test_patch_that_does_not_fix_the_bug_is_rejected():
    result = check(USELESS)
    assert not result.passed
    assert "fail with the patch" in result.reason


@needs_branch
def test_patch_with_syntax_error_is_rejected():
    result = check(BROKEN)
    assert not result.passed
    assert "does not compile" in result.reason


def test_unreproducible_bug_cannot_be_validated():
    healthy = RunReport(mode="local", compiled=True, tests_passed=7, replay_statuses=[201, 401])
    result = judge(healthy, healthy, has_replay=True)
    assert not result.passed
    assert "could not be reproduced" in result.reason
