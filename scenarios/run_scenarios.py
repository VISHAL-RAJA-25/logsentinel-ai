"""Run bug scenarios: check out each branch in a temporary git worktree,
start the app, send requests, and save the log and ground truth to datasets/<id>/."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


def run_scenario(sc):
    tmp = Path(tempfile.mkdtemp(prefix="logsentinel_"))
    worktree = tmp / "repo"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), sc["branch"]],
        cwd=ROOT, check=True, capture_output=True,
    )
    log_dir = tmp / "logs"
    env = {
        **os.environ,
        "APP_LOG_DIR": str(log_dir),
        "DATABASE_URL": f"sqlite:///{tmp / 'demo.db'}",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "shop.main:app", "--port", str(PORT)],
        cwd=worktree / "demo_app",
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                httpx.get(f"{BASE}/health", timeout=1).raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError("Server did not start")

        for req in sc["requests"]:
            r = httpx.request(
                req["method"], BASE + req["path"], json=req.get("json"), timeout=10
            )
            print(f"  {req['method']} {req['path']} -> {r.status_code}")
        time.sleep(0.5)
    finally:
        server.terminate()
        server.wait(timeout=10)

    out = ROOT / "datasets" / sc["id"]
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(log_dir / "app.log", out / "app.log")
    truth = {"id": sc["id"], "branch": sc["branch"], "title": sc["title"],
             **sc["ground_truth"]}
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=2))

    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=ROOT, check=True, capture_output=True,
    )
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"  saved datasets/{sc['id']}/")


def main():
    scenarios = json.loads((ROOT / "scenarios" / "scenarios.json").read_text())
    wanted = sys.argv[1:]
    for sc in scenarios:
        if wanted and sc["id"] not in wanted:
            continue
        print(f"Scenario {sc['id']}: {sc['title']}")
        run_scenario(sc)


if __name__ == "__main__":
    main()
