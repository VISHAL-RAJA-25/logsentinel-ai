import json
from pathlib import Path

import pytest

from app.agents.log_parser import parse_log_file, parse_log_text

DATASETS = Path(__file__).resolve().parents[2] / "datasets"
FOLDERS = sorted(p for p in DATASETS.iterdir() if p.is_dir())


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_incident_matches_ground_truth(folder):
    truth = json.loads((folder / "ground_truth.json").read_text())
    incidents = parse_log_file(folder / "app.log")
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc.error_type == truth["error_type"]
    assert inc.file == truth["file"]
    assert inc.function == truth["function"]
    assert inc.line is not None


def test_duplicate_errors_are_counted():
    text = (DATASETS / "001" / "app.log").read_text()
    incidents = parse_log_text(text + "\n" + text)
    assert len(incidents) == 1
    assert incidents[0].frequency == 2


def test_log_without_errors_gives_no_incidents():
    text = "2026-10-24 10:11:52,227 | INFO | shop.user_service | Created user id=1 email=a@test.com\n"
    assert parse_log_text(text) == []
