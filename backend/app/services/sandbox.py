"""Sandbox: tests a proposed patch in an isolated environment before anyone sees it.

For the ORIGINAL code and the PATCHED code it runs the same checks:
  1. syntax check   2. the app's own test suite   3. lint (information only)
  4. replay of the request that failed in production.
The bug must reproduce on the original code, and the patched code must pass.

Docker mode (default): no network, memory/CPU/process limits, non-root user.
Local mode (SANDBOX_MODE=local): same checks in a plain subprocess. NOT isolated.
It exists for tests and CI only.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Optional

from app.agents.patch_generator import apply_edits_to_dir
from app.models.patch import PatchEdit, RunReport, ValidationResult

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "logsentinel-sandbox:latest"
TIMEOUT_S = 150


class SandboxUnavailable(Exception):
    pass


# This script runs INSIDE the sandbox, next to the exported demo_app folder.
CHECKS_PY = r'''
import json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
result = {"compiled": False, "tests_passed": 0, "tests_failed": 0, "replay_statuses": [],
          "replay_error": None, "lint_issues": None, "output_tail": ""}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=100)


def count(pattern, text):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


p = run([sys.executable, "-m", "compileall", "-q", "shop"])
result["compiled"] = p.returncode == 0
if not result["compiled"]:
    result["output_tail"] = (p.stdout + p.stderr)[-1500:]
else:
    t = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"])
    out = t.stdout + t.stderr
    result["tests_passed"] = count(r"(\d+) passed", out)
    result["tests_failed"] = count(r"(\d+) failed", out) + count(r"(\d+) error", out)
    result["output_tail"] = out[-1500:]
    lint = run([sys.executable, "-m", "ruff", "check", "--select", "F", "--output-format", "concise", "shop"])
    if "No module named" not in lint.stderr:
        result["lint_issues"] = len(re.findall(r":\d+:\d+:", lint.stdout))
    try:
        os.environ["DATABASE_URL"] = "sqlite:///./replay.db"
        os.environ["APP_LOG_DIR"] = "replay_logs"
        sys.path.insert(0, os.getcwd())
        from fastapi.testclient import TestClient
        from shop.main import app
        client = TestClient(app, raise_server_exceptions=False)
        with open(os.path.join(HERE, "replay.json")) as f:
            for req in json.load(f):
                response = client.request(req["method"], req["path"], json=req.get("json"))
                result["replay_statuses"].append(response.status_code)
    except Exception as exc:
        result["replay_error"] = (type(exc).__name__ + ": " + str(exc))[:300]
print("RESULT_JSON:" + json.dumps(result))
'''


def load_scenario(dataset_id: str) -> dict:
    for scenario in json.loads((ROOT / "scenarios" / "scenarios.json").read_text()):
        if scenario["id"] == dataset_id:
            return scenario
    raise KeyError(f"scenario {dataset_id} not found")


def export_tree(repo_path, ref: str, dest: Path) -> None:
    """Copy demo_app exactly as it is at `ref`, without touching the working copy."""
    out = subprocess.run(["git", "archive", "--format=tar", ref, "demo_app"],
                         cwd=repo_path, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(out)) as tar:
        tar.extractall(dest, filter="data")


def _prepare(ws: Path, repo_path, ref: str, requests: Optional[list],
             edits: Optional[list[PatchEdit]] = None) -> None:
    ws.mkdir(parents=True)
    export_tree(repo_path, ref, ws)
    if edits:
        apply_edits_to_dir(ws, edits)
    (ws / "checks.py").write_text(CHECKS_PY)
    (ws / "replay.json").write_text(json.dumps(requests or []))
    for dirpath, _, filenames in os.walk(ws):  # the sandbox user is not the owner
        os.chmod(dirpath, 0o777)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o666)


def _parse(stdout: str, mode: str, stderr: str = "", timed_out: bool = False) -> RunReport:
    for line in reversed(stdout.splitlines()):
        if line.startswith("RESULT_JSON:"):
            return RunReport(mode=mode, timed_out=timed_out, **json.loads(line[len("RESULT_JSON:"):]))
    return RunReport(mode=mode, timed_out=timed_out,
                     output_tail=(stdout + stderr)[-1500:] or "no output from the checks")


def run_locally(ws: Path) -> RunReport:
    try:
        proc = subprocess.run([sys.executable, str(ws / "checks.py")], cwd=ws / "demo_app",
                              capture_output=True, text=True, timeout=TIMEOUT_S)
        return _parse(proc.stdout, "local", proc.stderr)
    except subprocess.TimeoutExpired:
        return RunReport(mode="local", timed_out=True, output_tail="timed out")


def get_client():
    try:
        import docker
    except ImportError as exc:
        raise SandboxUnavailable("The 'docker' Python package is not installed.") from exc
    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception:
        alt = Path.home() / ".docker" / "run" / "docker.sock"  # Docker Desktop on Mac
        if alt.exists():
            try:
                client = docker.DockerClient(base_url=f"unix://{alt}")
                client.ping()
                return client
            except Exception:
                pass
    raise SandboxUnavailable("Docker is not reachable. Start Docker Desktop and try again.")


def run_in_docker(ws: Path) -> RunReport:
    import docker

    client = get_client()
    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound as exc:
        raise SandboxUnavailable(
            "Sandbox image not built yet. Run once: python -m app.services.sandbox build") from exc
    container = client.containers.run(
        IMAGE, ["python", "/work/checks.py"], detach=True,
        volumes={str(ws): {"bind": "/work", "mode": "rw"}},
        working_dir="/work/demo_app",
        network_mode="none",      # no internet, no other containers
        mem_limit="512m",
        nano_cpus=1_000_000_000,  # one CPU
        pids_limit=256,
        user="65534:65534",       # 'nobody', not root
        environment={"HOME": "/tmp"},
    )
    timed_out = False
    try:
        try:
            container.wait(timeout=TIMEOUT_S)
        except Exception:
            timed_out = True
            container.kill()
        logs = container.logs().decode("utf-8", errors="replace")
    finally:
        container.remove(force=True)
    return _parse(logs, "docker", "", timed_out)


def judge(baseline: RunReport, patched: RunReport, has_replay: bool) -> ValidationResult:
    reproduced = baseline.replay_has_5xx or baseline.tests_failed > 0
    tests_ok = patched.compiled and patched.tests_failed == 0 and patched.tests_passed > 0
    replay_ok = (not has_replay) or (
        bool(patched.replay_statuses) and not patched.replay_has_5xx and not patched.replay_error)
    if not reproduced:
        reason = "the bug could not be reproduced in the sandbox, so the patch cannot be validated"
    elif patched.timed_out:
        reason = "the patched code timed out in the sandbox"
    elif not patched.compiled:
        reason = "the patched code does not compile"
    elif not tests_ok:
        reason = f"{patched.tests_failed} test(s) fail with the patch"
    elif not replay_ok:
        reason = f"the failing request still misbehaves: statuses {patched.replay_statuses}"
    else:
        reason = "the patch compiles, the test suite passes and the failing request no longer errors"
    passed = reproduced and tests_ok and replay_ok and not patched.timed_out
    return ValidationResult(passed=passed, mode=patched.mode, reason=reason,
                            bug_reproduced=reproduced, baseline=baseline, patched=patched)


def _runner(mode: Optional[str]) -> Callable[[Path], RunReport]:
    return run_locally if (mode or os.getenv("SANDBOX_MODE", "docker")) == "local" else run_in_docker


def validate_patch(edits: list[PatchEdit], repo_path, ref: str,
                   requests: Optional[list] = None, mode: Optional[str] = None) -> ValidationResult:
    runner = _runner(mode)
    base = ROOT / ".sandbox"
    base.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="run_", dir=base))
    try:
        _prepare(tmp / "baseline", repo_path, ref, requests)
        _prepare(tmp / "patched", repo_path, ref, requests, edits)
        baseline = runner(tmp / "baseline")
        patched = runner(tmp / "patched")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return judge(baseline, patched, has_replay=bool(requests))


def describe_failure(edits_diff: str, result: ValidationResult) -> str:
    """Text fed back to the Patch Generator after a rejected attempt."""
    p = result.patched
    return (
        f"Your patch:\n{edits_diff}\n"
        f"Sandbox verdict: {result.reason}.\n"
        f"Tests with your patch: {p.tests_passed} passed, {p.tests_failed} failed.\n"
        f"Replay of the failing request returned statuses: {p.replay_statuses}"
        f"{' (error: ' + p.replay_error + ')' if p.replay_error else ''}.\n"
        f"Test output (tail):\n{p.output_tail[-800:]}"
    )


def build_image() -> None:
    client = get_client()
    print("Building the sandbox image (needs internet, takes a minute) ...")
    client.images.build(path=str(ROOT / "sandbox"), tag=IMAGE, rm=True)
    print(f"Built {IMAGE}")


def smoke(dataset_id: str) -> None:
    """Run the checks on the ORIGINAL buggy code of one dataset, with no patch."""
    scenario = load_scenario(dataset_id)
    runner = _runner(None)
    base = ROOT / ".sandbox"
    base.mkdir(exist_ok=True)
    ws = base / f"smoke_{dataset_id}"
    shutil.rmtree(ws, ignore_errors=True)
    _prepare(ws, ROOT, scenario["branch"], scenario["requests"])
    try:
        report = runner(ws)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "build":
        build_image()
    elif command == "smoke" and len(sys.argv) > 2:
        smoke(sys.argv[2])
    else:
        sys.exit("Usage: python -m app.services.sandbox build\n"
                 "       python -m app.services.sandbox smoke <dataset id>")
