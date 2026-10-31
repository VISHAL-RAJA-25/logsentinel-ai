import copy
import json

import pytest

from app.agents.classifier import Classification
from app.agents.log_parser import parse_log_text
from app.agents.root_cause import AnalysisError, analyze, extract_json

LOG = (
    "2026-10-24 16:49:32,683 | ERROR | shop.api | Unhandled error on POST /login\n"
    "Traceback (most recent call last):\n"
    '  File "/a/demo_app/shop/user_service.py", line 16, in authenticate\n'
    "    if user.password_hash != hash_password(password):\n"
    "AttributeError: 'NoneType' object has no attribute 'password_hash'\n"
)
INCIDENT = parse_log_text(LOG)[0]
CLASSIFICATION = Classification(category="Authentication", source="rules", reason="test")
CONTEXT = ">>   16 |     if user.password_hash != hash_password(password):"

GOOD = {
    "summary": "Login crashes for unknown emails.",
    "hypotheses": [
        {"cause": "Unlikely alternative", "confidence": 20,
         "evidence": ["something `that is not in the material`"],
         "suggested_fix": "n/a", "risk_level": "low", "files_involved": []},
        {"cause": "user is None for an unknown email", "confidence": 90,
         "evidence": ["`user.password_hash != hash_password(password)` reads user without a check"],
         "suggested_fix": "Add a None check in authenticate", "risk_level": "low",
         "files_involved": ["demo_app/shop/user_service.py"]},
    ],
}


def fake_llm(*replies):
    calls = []

    def llm(messages):
        calls.append(messages)
        return replies[min(len(calls), len(replies)) - 1]

    llm.calls = calls
    return llm


def run(llm):
    return analyze(INCIDENT, CLASSIFICATION, CONTEXT, llm=llm)


def test_valid_reply_is_sorted_and_grounded():
    result = run(fake_llm(json.dumps(GOOD)))
    best, other = result.hypotheses
    assert best.confidence == 90
    assert (best.quotes_total, best.quotes_verified) == (1, 1)
    assert (other.quotes_total, other.quotes_verified) == (1, 0)  # made-up quote is caught


def test_code_fences_are_stripped():
    result = run(fake_llm("```json\n" + json.dumps(GOOD) + "\n```"))
    assert len(result.hypotheses) == 2


def test_invalid_reply_is_retried():
    llm = fake_llm("this is not json", json.dumps(GOOD))
    assert len(run(llm).hypotheses) == 2
    assert len(llm.calls) == 2


def test_gives_up_after_two_bad_replies():
    with pytest.raises(AnalysisError):
        run(fake_llm("nope", "still nope"))


def test_fractional_confidence_is_scaled():
    data = copy.deepcopy(GOOD)
    data["hypotheses"][1]["confidence"] = 0.9
    assert run(fake_llm(json.dumps(data))).hypotheses[0].confidence == 90


def test_more_than_three_hypotheses_are_trimmed():
    data = copy.deepcopy(GOOD)
    data["hypotheses"] = [copy.deepcopy(GOOD["hypotheses"][1]) for _ in range(5)]
    assert len(run(fake_llm(json.dumps(data))).hypotheses) == 3


def test_extract_json_ignores_text_around_it():
    assert extract_json('Sure! {"a": 1} Hope that helps.') == {"a": 1}


def test_prompt_contains_incident_and_context():
    llm = fake_llm(json.dumps(GOOD))
    run(llm)
    user_message = llm.calls[0][1]["content"]
    assert "AttributeError" in user_message
    assert CONTEXT in user_message
