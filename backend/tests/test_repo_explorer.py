import json
from pathlib import Path

import git
import pytest

from app.agents.log_parser import parse_log_file, parse_log_text
from app.agents.repo_explorer import explore

ROOT = Path(__file__).resolve().parents[2]
FOLDERS = sorted(p for p in (ROOT / "datasets").iterdir() if p.is_dir())


def ref_exists(name: str) -> bool:
    try:
        git.Repo(ROOT).commit(name)
        return True
    except Exception:
        return False


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_context_for_each_dataset(folder):
    truth = json.loads((folder / "ground_truth.json").read_text())
    if not ref_exists(truth["branch"]):
        pytest.skip(f"branch {truth['branch']} is not available locally")
    incident = parse_log_file(folder / "app.log")[0]
    ctx = explore(incident, ref=truth["branch"], repo_path=ROOT)

    # the failing line is shown, marked with >>
    assert ctx.function_source and ">>" in ctx.function_source
    assert incident.code_line in ctx.function_source
    # the newest commit on the deployed version touched the file that caused the bug
    introduced = truth.get("introduced_in", truth["file"])
    assert introduced in ctx.recent_commits[0].files
    assert ctx.recent_commits[0].diff


def test_missing_file_still_returns_commit_history():
    log = (
        "2026-10-24 10:00:00,000 | ERROR | shop.api | Unhandled error on GET /x\n"
        "Traceback (most recent call last):\n"
        '  File "/a/demo_app/shop/does_not_exist.py", line 3, in foo\n'
        '    raise ValueError("boom")\n'
        "ValueError: boom\n"
    )
    ctx = explore(parse_log_text(log)[0], ref="main", repo_path=ROOT)
    assert ctx.function_source is None
    assert len(ctx.recent_commits) > 0
