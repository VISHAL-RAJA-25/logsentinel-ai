from pathlib import Path

import git
import pytest

import json

from app.agents.log_parser import parse_log_text
from app.agents.patch_review import ai_review, review_files, review_patch
from app.models.analysis import Hypothesis
from app.models.patch import PatchEdit
from app.services.sandbox import load_scenario

ROOT = Path(__file__).resolve().parents[2]
PAY = "demo_app/shop/payment_service.py"
A = "demo_app/shop/a.py"
BEFORE = {A: 'import os\n\n\ndef f():\n    key = os.environ["API_KEY"]\n    call(key)\n'}


def test_new_except_block_that_never_raises_is_flagged():
    after = {A: 'import os\n\n\ndef f():\n    key = os.environ["API_KEY"]\n    try:\n        call(key)\n'
                '    except OSError:\n        log("failed")\n'}
    warnings = review_files(BEFORE, after)
    assert len(warnings) == 1 and "never re-raises" in warnings[0]


def test_new_except_block_that_raises_is_fine():
    after = {A: 'import os\n\n\ndef f():\n    key = os.environ["API_KEY"]\n    try:\n        call(key)\n'
                '    except OSError as exc:\n        raise RuntimeError("service down") from exc\n'}
    assert review_files(BEFORE, after) == []


def test_required_environment_variable_made_optional_is_flagged():
    after = {A: 'import os\n\n\ndef f():\n    key = os.getenv("API_KEY")\n    call(key)\n'}
    warnings = review_files(BEFORE, after)
    assert len(warnings) == 1 and "optional" in warnings[0]


def test_existing_except_block_is_not_blamed_on_the_patch():
    before = {A: 'def f():\n    try:\n        call(1)\n    except OSError:\n        log("x")\n'}
    after = {A: 'def f():\n    try:\n        call(2)\n    except OSError:\n        log("x")\n'}
    assert review_files(before, after) == []


def test_clean_patch_has_no_warnings():
    after = {A: 'import os\n\n\ndef f():\n    key = os.environ["API_KEY"]\n    call(key, retries=3)\n'}
    assert review_files(BEFORE, after) == []


# The two fake fixes the pipeline really produced and the first sandbox wrongly called "validated".
REAL_MASKING_PATCHES = [
    ("003", PatchEdit(file=PAY, old='    api_key = os.environ["PAYMENT_API_KEY"]',
                      new='    api_key = os.getenv("PAYMENT_API_KEY")'), "optional"),
    ("011", PatchEdit(
        file=PAY,
        old='    httpx.post(f"{GATEWAY_URL}/charge", json={"user_id": user_id, "amount": amount}, timeout=3)',
        new='    try:\n        httpx.post(f"{GATEWAY_URL}/charge", json={"user_id": user_id, "amount": amount}, timeout=3)\n'
            '    except httpx.HTTPError as exc:\n        logger.warning("Payment gateway request failed: %s", exc)'),
     "never re-raises"),
    ("011", PatchEdit(
        file=PAY,
        old='    httpx.post(f"{GATEWAY_URL}/charge", json={"user_id": user_id, "amount": amount}, timeout=3)',
        new="    # Simulate successful gateway call\n    pass"), "no longer contacts"),
    ("011", PatchEdit(
        file=PAY,
        old='    httpx.post(f"{GATEWAY_URL}/charge", json={"user_id": user_id, "amount": amount}, timeout=3)',
        new="    # Simulate successful gateway call\n    pass"), "no-op"),
]


@pytest.mark.parametrize("dataset_id, edit, expected", REAL_MASKING_PATCHES, ids=lambda v: v if isinstance(v, str) and v.isdigit() else "")
def test_masking_patches_from_real_runs_are_flagged(dataset_id, edit, expected):
    branch = load_scenario(dataset_id)["branch"]
    try:
        git.Repo(ROOT).commit(branch)
    except Exception:
        pytest.skip(f"branch {branch} not available locally")
    warnings = review_patch([edit], ROOT, branch)
    assert any(expected in w for w in warnings), warnings


def test_replacing_code_with_pass_is_flagged():
    before = {A: "def f():\n    call()\n"}
    after = {A: "def f():\n    pass\n"}
    assert "no-op" in review_files(before, after)[0]


def test_removing_an_external_call_is_flagged_but_changing_it_is_not():
    before = {A: "import httpx\n\n\ndef f():\n    httpx.post(URL)\n"}
    removed = {A: "import httpx\n\n\ndef f():\n    return 1\n"}
    changed = {A: "import httpx\n\n\ndef f():\n    httpx.post(URL, timeout=5)\n"}
    assert any("no longer contacts" in w for w in review_files(before, removed))
    assert review_files(before, changed) == []


LOG = (
    "2026-10-24 16:49:32,683 | ERROR | shop.api | Unhandled error on POST /payments\n"
    "Traceback (most recent call last):\n"
    '  File "/a/demo_app/shop/payment_service.py", line 16, in create_payment\n'
    "    httpx.post(URL)\n"
    "httpx.ConnectError: Connection refused\n"
)
INCIDENT = parse_log_text(LOG)[0]
HYPOTHESIS = Hypothesis(cause="the gateway is down", confidence=90, evidence=["`x`"],
                        suggested_fix="x", risk_level="low")


def test_ai_review_flags_a_patch_that_masks_the_symptom():
    llm = lambda messages: json.dumps({"verdict": "masks_symptom", "reason": "The gateway call became pass."})
    assert ai_review(INCIDENT, HYPOTHESIS, "diff", llm) == "AI review: The gateway call became pass."


def test_ai_review_stays_quiet_for_a_real_fix_or_a_broken_reply():
    fine = lambda messages: json.dumps({"verdict": "root_cause_fix", "reason": "ok"})
    assert ai_review(INCIDENT, HYPOTHESIS, "diff", fine) is None
    assert ai_review(INCIDENT, HYPOTHESIS, "diff", lambda messages: "not json at all") is None
