"""Git Agent: turns a VALIDATED patch into a DRAFT pull request. It never merges.

Safety gates, all enforced in code:
  * only patches with status 'validated' are accepted (not needs_review, rejected or unvalidated)
  * the patch may only touch demo_app/shop/
  * GITHUB_REPO must match this repository's own 'origin'
  * dry run by default: nothing happens without --create
  * the pull request is always a draft, and this module contains no merge operation
It works in a temporary git worktree, so your working copy is never touched.
"""
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

from app.services.storage import Store

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
API = "https://api.github.com"
ALLOWED_PREFIX = "demo_app/shop/"
BANNER = ("> **Draft pull request opened automatically by LogSentinel AI.** The patch passed an isolated "
          "sandbox, but a human must review it before merging.\n\n")


class GitAgentError(Exception):
    pass


@dataclass
class Plan:
    investigation_id: int
    branch: str
    base: str
    title: str
    body: str
    diff: str
    files: list[str]


def changed_files(diff: str) -> list[str]:
    return re.findall(r"^\+\+\+ b/(\S+)", diff, re.MULTILINE)


def make_plan(investigation: dict) -> Plan:
    inv_id, status = investigation["id"], investigation.get("patch_status")
    if status != "validated":
        raise GitAgentError(f"investigation #{inv_id} has patch status '{status}'. "
                            "Only 'validated' patches may become pull requests.")
    diff = investigation.get("patch_diff") or ""
    files = changed_files(diff)
    outside = [f for f in files if not f.startswith(ALLOWED_PREFIX)]
    if not files or outside:
        raise GitAgentError(f"the patch touches files outside {ALLOWED_PREFIX}: {outside or 'no files found'}")
    incident = investigation.get("incident") or {}
    title = (f"[LogSentinel] Fix {incident.get('error_type', 'error')} in "
             f"{incident.get('function', 'code')}() (incident #{inv_id})")
    return Plan(investigation_id=inv_id, branch=f"logsentinel/fix-{inv_id}", base=investigation["ref"],
                title=title, body=(BANNER + (investigation.get("report_md") or ""))[:60000],
                diff=diff, files=files)


def origin_repo(repo_path) -> str:
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_path,
                         capture_output=True, text=True, check=True).stdout.strip()
    found = re.search(r"github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?$", url)
    if not found:
        raise GitAgentError(f"origin is not a GitHub repository: {url}")
    return found.group("repo")


def git_env(token: Optional[str]) -> dict:
    """Authenticate git over HTTPS without putting the token in a URL or on the command line."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"})
    return env


def run(cmd: list[str], cwd, env: Optional[dict] = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitAgentError(f"`git {cmd[1]}` failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


def create_pull_request(plan: Plan, token: str, repo: str, repo_path=ROOT,
                        remote_url: Optional[str] = None) -> str:
    remote = remote_url or f"https://github.com/{repo}.git"
    env = git_env(token)
    if not run(["git", "ls-remote", "--heads", remote, plan.base], repo_path, env).stdout.strip():
        raise GitAgentError(f"the base branch '{plan.base}' does not exist on GitHub. Push it first.")
    if run(["git", "ls-remote", "--heads", remote, plan.branch], repo_path, env).stdout.strip():
        raise GitAgentError(f"branch '{plan.branch}' already exists on GitHub. A pull request was probably opened before.")
    if run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{plan.branch}"], repo_path, check=False).returncode == 0:
        raise GitAgentError(f"a local branch '{plan.branch}' already exists. Delete it first: git branch -D {plan.branch}")
    start = plan.base if run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{plan.base}"],
                             repo_path, check=False).returncode == 0 else f"origin/{plan.base}"

    tmp = Path(tempfile.mkdtemp(prefix="logsentinel_pr_"))
    worktree = tmp / "wt"
    try:
        run(["git", "worktree", "add", "-b", plan.branch, str(worktree), start], repo_path)
        patch_file = tmp / "fix.patch"
        patch_file.write_text(plan.diff)
        run(["git", "apply", "--index", str(patch_file)], worktree)
        message = (f"{plan.title}\n\nGenerated by LogSentinel AI from incident #{plan.investigation_id}. "
                   "Validated in an isolated sandbox. Review before merging.")
        run(["git", "-c", "user.name=LogSentinel AI", "-c", "user.email=logsentinel-ai@users.noreply.github.com",
             "commit", "-m", message], worktree)
        run(["git", "push", remote, f"{plan.branch}:{plan.branch}"], worktree, env)
    finally:
        run(["git", "worktree", "remove", "--force", str(worktree)], repo_path, check=False)
        run(["git", "branch", "-D", plan.branch], repo_path, check=False)
        shutil.rmtree(tmp, ignore_errors=True)

    response = requests.post(
        f"{API}/repos/{repo}/pulls",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": plan.title, "head": plan.branch, "base": plan.base, "body": plan.body, "draft": True},
        timeout=30,
    )
    if response.status_code != 201:
        detail = response.json().get("message", "") if response.content else ""
        raise GitAgentError(f"GitHub refused to create the pull request ({response.status_code}): {detail}")
    return response.json()["html_url"]


def main(argv: list[str]) -> None:
    flags = {a for a in argv if a.startswith("--")}
    ids = [a for a in argv if not a.startswith("--")]
    if len(ids) != 1 or not ids[0].isdigit():
        sys.exit("Usage: python -m app.agents.git_agent <investigation id> [--create]")
    store = Store()
    investigation = store.get(int(ids[0]))
    if not investigation:
        sys.exit(f"No investigation with id {ids[0]}. Run: python -m app.graph.pipeline --list")
    try:
        plan = make_plan(investigation)
        repo, token = os.getenv("GITHUB_REPO"), os.getenv("GITHUB_TOKEN")
        origin = origin_repo(ROOT)
        if not repo or repo.lower() != origin.lower():
            raise GitAgentError(f"GITHUB_REPO ('{repo}') does not match this repository's origin ('{origin}')")
        print(f"Plan for investigation #{plan.investigation_id}")
        print(f"  title : {plan.title}")
        print(f"  branch: {plan.branch}  ->  pull request into '{plan.base}'  (DRAFT)")
        print(f"  files : {', '.join(plan.files)}")
        print("\n" + plan.diff)
        if "--create" not in flags:
            print("Dry run. Nothing was changed. Add --create to open the draft pull request.")
            return
        if not token:
            raise GitAgentError("GITHUB_TOKEN is missing in .env")
        url = create_pull_request(plan, token, repo)
        store.set_pr_url(plan.investigation_id, url)
        print(f"\nDraft pull request opened: {url}\nA human must review it. It was NOT merged.")
    except GitAgentError as exc:
        sys.exit(f"Refused: {exc}")


if __name__ == "__main__":
    main(sys.argv[1:])
