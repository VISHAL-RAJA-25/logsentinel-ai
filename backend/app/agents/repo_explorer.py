"""Repo Explorer: gathers only the code and history relevant to one incident.

Reads files from a git ref (the deployed version), so it sees the code that
was actually running, not whatever is checked out right now.
"""
import ast
import sys
from pathlib import Path
from typing import Optional

import git

from app.agents.log_parser import parse_log_file
from app.models.context import BlameInfo, CommitInfo, RepoContext
from app.models.incident import Incident

DEFAULT_REPO = Path(__file__).resolve().parents[3]
PROJECT_DIR = "demo_app"
MAX_DIFF_CHARS = 4000
WINDOW = 10  # lines shown around the failure when no function is found


def read_file_at_ref(repo: git.Repo, ref: str, path: str) -> Optional[str]:
    try:
        return repo.git.show(f"{ref}:{path}")
    except git.GitCommandError:
        return None


def find_function(tree: ast.AST, name: str, line: int):
    """Innermost function called `name` whose body contains `line`."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            if node.lineno <= line <= node.end_lineno:
                if best is None or (node.end_lineno - node.lineno) < (best.end_lineno - best.lineno):
                    best = node
    return best


def numbered(lines: list[str], start: int, end: int, mark: int) -> str:
    return "\n".join(
        f"{'>>' if n == mark else '  '} {n:4d} | {lines[n - 1]}"
        for n in range(start, end + 1)
    )


def _node_text(lines: list[str], node: ast.AST, with_comments: bool = False) -> str:
    start = node.lineno
    while with_comments and start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return "\n".join(lines[start - 1:node.end_lineno])


def module_level_info(tree: ast.Module, lines: list[str]):
    imports, constants = [], []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(_node_text(lines, node))
        elif isinstance(node, ast.Assign) and all(
            isinstance(t, ast.Name) and t.id.isupper() for t in node.targets
        ):
            constants.append(_node_text(lines, node, with_comments=True))
    return imports, constants


def blame_line(repo: git.Repo, ref: str, path: str, line: int) -> Optional[BlameInfo]:
    try:
        out = repo.git.blame("-L", f"{line},{line}", "--porcelain", ref, "--", path)
    except git.GitCommandError:
        return None
    rows = out.splitlines()
    sha = rows[0].split()[0]
    author = summary = ""
    for row in rows[1:]:
        if row.startswith("author "):
            author = row[len("author "):]
        elif row.startswith("summary "):
            summary = row[len("summary "):]
    commit = repo.commit(sha)
    return BlameInfo(sha=sha[:8], author=author, date=commit.committed_datetime, summary=summary)


def recent_commits(repo: git.Repo, ref: str, max_commits: int) -> list[CommitInfo]:
    """Latest commits that touched the app, across ALL its files. The cause of
    an incident is often in a file that is not in the stack trace."""
    commits = []
    for c in repo.iter_commits(ref, paths=PROJECT_DIR, max_count=max_commits):
        files = repo.git.show(c.hexsha, "--name-only", "--format=", "--", PROJECT_DIR).splitlines()
        diff = repo.git.show(c.hexsha, "--format=", "--", PROJECT_DIR)
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]"
        commits.append(CommitInfo(
            sha=c.hexsha[:8], author=c.author.name, date=c.committed_datetime,
            message=c.summary, files=files, diff=diff,
        ))
    return commits


def explore(incident: Incident, ref: str = "HEAD", repo_path=DEFAULT_REPO,
            max_commits: int = 5) -> RepoContext:
    repo = git.Repo(repo_path, search_parent_directories=True)
    ctx = RepoContext(
        ref=ref,
        head_sha=repo.commit(ref).hexsha[:8],
        culprit_file=incident.file,
        culprit_function=incident.function,
        failing_line=incident.line,
        call_chain=[
            f"{f.file}:{f.line} in {f.function}() -> {f.code}"
            for f in incident.frames if f.is_project_code
        ],
        recent_commits=recent_commits(repo, ref, max_commits),
    )
    if incident.file and incident.line:
        source = read_file_at_ref(repo, ref, incident.file)
        if source:
            lines = source.splitlines()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                tree = None
            if tree:
                ctx.imports, ctx.module_constants = module_level_info(tree, lines)
                node = find_function(tree, incident.function or "", incident.line)
                if node:
                    ctx.function_source = numbered(lines, node.lineno, node.end_lineno, incident.line)
            if ctx.function_source is None:  # module level, lambda, comprehension ...
                start = max(1, incident.line - WINDOW)
                end = min(len(lines), incident.line + WINDOW)
                ctx.function_source = numbered(lines, start, end, incident.line)
        ctx.blame = blame_line(repo, ref, incident.file, incident.line)
    return ctx


def format_context(ctx: RepoContext, diff_commits: int = 2) -> str:
    """Readable text. Step 6 will paste this into the LLM prompt."""
    parts = [f"Deployed version: {ctx.ref} ({ctx.head_sha})"]
    if ctx.call_chain:
        parts.append("Call chain (project code, oldest first):\n" + "\n".join("  " + c for c in ctx.call_chain))
    if ctx.function_source:
        parts.append(f"Failing code in {ctx.culprit_file} ('>>' marks the failing line):\n{ctx.function_source}")
    if ctx.module_constants:
        parts.append("Module-level constants in that file:\n" + "\n".join(ctx.module_constants))
    if ctx.imports:
        parts.append("Imports in that file:\n" + "\n".join(ctx.imports))
    if ctx.blame:
        b = ctx.blame
        parts.append(f"Failing line last changed by: {b.sha} '{b.summary}' ({b.author}, {b.date:%Y-%m-%d})")
    for n, c in enumerate(ctx.recent_commits):
        head = f"Recent commit {c.sha} '{c.message}' ({c.author}, {c.date:%Y-%m-%d %H:%M})\n  files: {', '.join(c.files)}"
        parts.append(head + (f"\n{c.diff}" if n < diff_commits else ""))
    return "\n\n".join(parts)


if __name__ == "__main__":
    log_path, ref = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "HEAD")
    for inc in parse_log_file(log_path):
        print(format_context(explore(inc, ref=ref)))
