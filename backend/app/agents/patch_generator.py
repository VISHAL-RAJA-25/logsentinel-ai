"""Patch Generator: turns the top hypothesis into a concrete code change.

The LLM does NOT write a diff (models get line numbers and whitespace wrong).
It returns exact search-and-replace edits. We check them against the real file
and generate the unified diff ourselves.
"""
import difflib
import re
from pathlib import Path
from typing import Callable, Optional

import git

from app.agents.repo_explorer import read_file_at_ref
from app.agents.root_cause import default_llm, extract_json
from app.models.analysis import Hypothesis
from app.models.incident import Incident
from app.models.patch import PatchEdit, PatchProposal

ALLOWED_PREFIX = "demo_app/shop/"  # tests and everything else are off limits
MAX_FILE_CHARS = 8000

SYSTEM_PROMPT = """You are a senior engineer fixing a production bug in a Python/FastAPI service.
Propose the smallest safe code change that fixes the diagnosed root cause.

Rules:
- Change only application files under demo_app/shop/. Never edit tests.
- Express each change as an exact search-and-replace on the CURRENT file content shown to you:
  the SEARCH text must be copied exactly (including indentation) and must occur exactly once in that file.
- Use 1 to 3 edits. Only edit files that are shown to you.
- Fix the behaviour, do not hide the error. For example, return a proper HTTP error response
  (fastapi.HTTPException with a 4xx status) for bad input instead of letting the server crash.
- Never hide a failure: do not wrap the failing call in a try/except that carries on normally, and do not turn
  a required setting into an optional one. If something outside the code is broken (a missing secret, a
  service that is down), raise fastapi.HTTPException (for example 503 with a clear message).
- Never invent hostnames, URLs, credentials or packages that do not appear in the material you were given.
- If the real fix is outside the code (a missing secret, an uninstalled package), still propose the
  best code-side mitigation and say so in the explanation.

Reply in exactly this format and nothing else. The code is plain text, so no quoting or escaping is needed.
Do not use JSON and do not use markdown fences.

EXPLANATION: <one or two sentences: what the patch does and why>

FILE: demo_app/shop/example.py
<<<<<<< SEARCH
<exact existing text>
=======
<replacement text>
>>>>>>> REPLACE

Repeat the FILE / SEARCH / REPLACE group for each extra edit."""

BLOCK = re.compile(
    r"FILE:[ \t]*(?P<file>\S+)[ \t]*\n<<<<<<< SEARCH\n(?P<old>.*?)\n=======\n(?P<new>.*?)\n?>>>>>>> REPLACE",
    re.DOTALL,
)


def parse_reply(raw: str) -> dict:
    """Understand the model's reply. SEARCH/REPLACE blocks are expected; a JSON object is still accepted."""
    if "<<<<<<< SEARCH" not in raw:
        return extract_json(raw)
    edits = [{"file": m.group("file"), "old": m.group("old"), "new": m.group("new")}
             for m in BLOCK.finditer(raw)]
    if not edits:
        raise ValueError("no complete FILE / SEARCH / ======= / REPLACE block found")
    found = re.search(r"EXPLANATION:\s*(.+?)\s*(?=FILE:)", raw, re.DOTALL)
    return {"explanation": found.group(1).strip() if found else "Patch proposed by the model.",
            "edits": edits}


class PatchError(Exception):
    pass


def check_edit_path(path: str) -> None:
    if not path.startswith(ALLOWED_PREFIX) or ".." in path or not path.endswith(".py"):
        raise PatchError(f"edits are only allowed in {ALLOWED_PREFIX}*.py files, not {path!r}")


def apply_edits(files: dict[str, str], edits: list[PatchEdit]) -> dict[str, str]:
    """Apply search-and-replace edits to file contents. Raises PatchError if an edit is invalid."""
    result = dict(files)
    for edit in edits:
        check_edit_path(edit.file)
        if edit.file not in result:
            raise PatchError(f"{edit.file} was not shown to you. Edit only the files provided.")
        count = result[edit.file].count(edit.old)
        if count != 1:
            raise PatchError(
                f"'old' must appear exactly once in {edit.file}, but it appears {count} times. "
                "Copy it exactly from the file shown."
            )
        result[edit.file] = result[edit.file].replace(edit.old, edit.new, 1)
    return result


def apply_edits_to_dir(root: Path, edits: list[PatchEdit]) -> None:
    """Apply edits to real files on disk (root contains the demo_app/ folder)."""
    files = {}
    for edit in edits:
        check_edit_path(edit.file)
        path = root / edit.file
        if edit.file not in files and path.exists():
            files[edit.file] = path.read_text()
    for path, text in apply_edits(files, edits).items():
        (root / path).write_text(text)


def make_diff(before: dict[str, str], after: dict[str, str]) -> str:
    chunks = []
    for path in sorted(after):
        if before[path] != after[path]:
            chunks.append("".join(difflib.unified_diff(
                before[path].splitlines(keepends=True),
                after[path].splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}",
            )))
    return "".join(chunks)


def files_to_show(incident: Incident, hypothesis: Hypothesis) -> list[str]:
    paths: list[str] = []
    for path in [incident.file, *hypothesis.files_involved]:
        if path and path.startswith(ALLOWED_PREFIX) and path.endswith(".py") and path not in paths:
            paths.append(path)
    return paths[:3]


def load_files(repo_path, ref: str, paths: list[str]) -> dict[str, str]:
    repo = git.Repo(repo_path, search_parent_directories=True)
    files = {}
    for path in paths:
        text = read_file_at_ref(repo, ref, path)
        if text is not None:
            files[path] = text if text.endswith("\n") else text + "\n"
    return files


def build_prompt(incident: Incident, hypothesis: Hypothesis, files: dict[str, str],
                 feedback: Optional[str]) -> str:
    shown = "\n\n".join(f"=== {path} ===\n{text[:MAX_FILE_CHARS]}" for path, text in files.items())
    parts = [
        "INCIDENT",
        f"Error: {incident.error_type_full}: {incident.message[:400]}",
        f"Request: {incident.request}",
        "",
        "DIAGNOSED ROOT CAUSE",
        hypothesis.cause,
        f"Suggested fix: {hypothesis.suggested_fix}",
        "",
        "CURRENT FILES (exact content)",
        shown,
    ]
    if feedback:
        parts += ["", "YOUR PREVIOUS ATTEMPT FAILED", feedback, "Propose a different, corrected patch."]
    return "\n".join(parts)


def generate_patch(incident: Incident, hypothesis: Hypothesis, repo_path, ref: str,
                   llm: Callable[[list[dict]], str] = default_llm,
                   feedback: Optional[str] = None, max_attempts: int = 3) -> PatchProposal:
    files = load_files(repo_path, ref, files_to_show(incident, hypothesis))
    if not files:
        raise PatchError("none of the involved files could be read at this version")
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(incident, hypothesis, files, feedback)}]
    last_error = None
    for _ in range(max_attempts):
        raw = llm(messages)
        try:
            data = parse_reply(raw)
            data["edits"] = data.get("edits", [])[:3]
            proposal = PatchProposal.model_validate(data)
            proposal.diff = make_diff(files, apply_edits(files, proposal.edits))
            if not proposal.diff:
                raise PatchError("the patch changes nothing")
            return proposal
        except (ValueError, TypeError, AttributeError, PatchError) as exc:  # includes ValidationError
            last_error = exc
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Your reply was rejected: {str(exc)[:400]}\n"
                                            "Reply again in the required EXPLANATION / FILE / SEARCH / REPLACE format only."},
            ]
    raise PatchError(f"no usable patch after {max_attempts} attempts: {last_error}")
