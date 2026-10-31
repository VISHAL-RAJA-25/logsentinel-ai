import json
from pathlib import Path

import pytest

from app.agents.classifier import classify
from app.agents.log_parser import parse_log_file, parse_log_text

DATASETS = Path(__file__).resolve().parents[2] / "datasets"
FOLDERS = sorted(p for p in DATASETS.iterdir() if p.is_dir())


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_rules_are_never_confidently_wrong(folder):
    truth = json.loads((folder / "ground_truth.json").read_text())
    incident = parse_log_file(folder / "app.log")[0]
    result = classify(incident, use_llm=False)
    # Abstaining (None) is allowed. A wrong answer is not.
    assert result.category in (None, truth["category"])


def test_rules_cover_most_incidents():
    covered = sum(
        classify(parse_log_file(f / "app.log")[0], use_llm=False).category is not None
        for f in FOLDERS
    )
    assert covered >= 12, f"rules only covered {covered} of {len(FOLDERS)}"


def test_unknown_error_abstains():
    log = (
        "2026-10-24 10:00:00,000 | ERROR | shop.api | Unhandled error on GET /x\n"
        "Traceback (most recent call last):\n"
        '  File "/a/demo_app/shop/foo.py", line 3, in foo\n'
        '    raise ValueError("boom")\n'
        "ValueError: boom\n"
    )
    result = classify(parse_log_text(log)[0], use_llm=False)
    assert result.category is None
    assert result.source == "unclassified"
