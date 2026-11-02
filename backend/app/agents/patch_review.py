"""Patch Review: catches patches that pass the sandbox by HIDING the error.

A request that stops returning 500 is not proof of a fix. Wrapping the failing call in a
try/except that carries on also stops the crash, but it can record a payment that was never
charged. These deterministic checks flag the usual tricks so a human looks at them.
"""
import ast
import json
import re
from typing import Callable, Optional

from app.agents.patch_generator import apply_edits, load_files, make_diff
from app.agents.root_cause import extract_json
from app.models.analysis import Hypothesis
from app.models.incident import Incident
from app.models.patch import PatchEdit

EXTERNAL_CALL = re.compile(r"\b(?:httpx|requests|urllib3?|aiohttp|smtplib|boto3|subprocess|socket)\.\w+\(")

REVIEW_PROMPT = """You are a skeptical senior engineer reviewing an automatically generated patch before it reaches a human.
Decide whether the patch fixes the ROOT CAUSE or only makes the symptom disappear.

Signs of a patch that only masks the symptom: it deletes, skips or stubs out the code that failed; it swallows an
exception and carries on; it returns fake or default data; it hard-codes a value so a test passes; it removes
validation or a safety check; it records a result (for example a completed payment) for something that did not happen.

Reply with one JSON object only, no markdown fences:
{"verdict": "root_cause_fix" or "masks_symptom" or "unclear", "reason": "<one sentence>"}"""


def _handler_texts(source: str) -> set[str]:
    tree = ast.parse(source)
    return {ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)}


def review_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return a warning for every suspicious change between two versions of the same files."""
    warnings = []
    for path in sorted(after):
        if before.get(path) == after[path]:
            continue
        try:
            old_handlers = _handler_texts(before[path]) if path in before else set()
            tree = ast.parse(after[path])
        except SyntaxError:
            continue  # the sandbox already reports syntax errors
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if (ast.get_source_segment(after[path], node) or "") in old_handlers:
                continue  # existing code, not part of the patch
            if not any(isinstance(n, ast.Raise) for n in ast.walk(node)):
                warnings.append(f"{path}:{node.lineno}: the new except block never re-raises, "
                                "so the failure is hidden instead of fixed")
        diff = make_diff({path: before.get(path, "")}, {path: after[path]})
        removed = [l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        if any("os.environ[" in l for l in removed) and any(
                re.search(r"os\.getenv\(|os\.environ\.get\(", l) for l in added):
            warnings.append(f"{path}: a required environment variable became optional, "
                            "so a missing setting is no longer detected")
        for call in sorted({m.group(0) for line in removed for m in EXTERNAL_CALL.finditer(line)}):
            if call not in after[path]:
                warnings.append(f"{path}: the call to {call}...) was removed, "
                                "so the code no longer contacts that service")
        if removed and any(line.strip() == "pass" for line in added):
            warnings.append(f"{path}: code was replaced with a no-op (pass), "
                            "so the behaviour is skipped, not fixed")
    return warnings


def review_patch(edits: list[PatchEdit], repo_path, ref: str) -> list[str]:
    files = load_files(repo_path, ref, sorted({e.file for e in edits}))
    return review_files(files, apply_edits(files, edits))


def ai_review(incident: Incident, hypothesis: Hypothesis, diff: str,
              llm: Callable[[list[dict]], str]) -> Optional[str]:
    """Second opinion from the LLM. Returns a warning if it thinks the patch only masks the symptom.
    If the reviewer fails or is unsure, nothing is added (the rule-based review still applies)."""
    user = (f"Error: {incident.error_type_full}: {incident.message[:300]}\n"
            f"Diagnosed root cause: {hypothesis.cause}\n\nPATCH:\n{diff}")
    try:
        data = extract_json(llm([{"role": "system", "content": REVIEW_PROMPT},
                                 {"role": "user", "content": user}]))
    except (ValueError, TypeError, AttributeError):
        return None
    if str(data.get("verdict", "")).strip().lower() == "masks_symptom":
        return f"AI review: {str(data.get('reason', 'the patch only hides the symptom'))[:300]}"
    return None
