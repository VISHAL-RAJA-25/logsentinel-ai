"""Usage: python scenarios/make_bug.py 002 003 ...
Creates each bug's branch (from main), pushes it, and updates scenarios.json."""
import json
import subprocess
import sys
from pathlib import Path

from bugs import BUGS

ROOT = Path(__file__).resolve().parent.parent
SC_FILE = ROOT / "scenarios" / "scenarios.json"


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check,
                          capture_output=True, text=True)


def make_branch(bug_id, bug):
    name = bug["branch"]
    if git("rev-parse", "--verify", name, check=False).returncode == 0:
        print(f"  {name} already exists, skipping branch")
        return
    git("checkout", "main")
    git("checkout", "-b", name)
    try:
        for path, old, new in bug["edits"]:
            p = ROOT / path
            text = p.read_text()
            assert text.count(old) == 1, f"Pattern not found exactly once in {path}: {old!r}"
            p.write_text(text.replace(old, new))
            git("add", path)
        git("commit", "-m", bug["message"])
        git("push", "-u", "origin", name)
        print(f"  created and pushed {name}")
    except Exception:
        git("checkout", "-f", "main")
        git("branch", "-D", name)
        raise
    finally:
        git("checkout", "main")


def update_scenarios(ids):
    data = json.loads(SC_FILE.read_text()) if SC_FILE.exists() else []
    for bug_id in ids:
        bug = BUGS[bug_id]
        entry = {"id": bug_id, "branch": bug["branch"], "title": bug["title"],
                 "requests": bug["requests"], "ground_truth": bug["ground_truth"]}
        data = [d for d in data if d["id"] != bug_id] + [entry]
    data.sort(key=lambda d: d["id"])
    SC_FILE.write_text(json.dumps(data, indent=2))


if __name__ == "__main__":
    ids = sys.argv[1:] or sorted(BUGS)
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        sys.exit("You have uncommitted changes. Commit them first, then retry.")
    for bug_id in ids:
        print(f"Bug {bug_id}: {BUGS[bug_id]['title']}")
        make_branch(bug_id, BUGS[bug_id])
    update_scenarios(ids)
    print("scenarios.json updated")
