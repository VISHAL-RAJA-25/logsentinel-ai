"""Turns a finished investigation into a Markdown report (no LLM call needed)."""


def build_report(state: dict) -> str:
    inc, cls, analysis = state["incident"], state["classification"], state["analysis"]
    first_line = inc.message.splitlines()[0][:200] if inc.message else ""
    lines = [
        f"# Incident report: {inc.error_type} in {inc.function or 'unknown'}()",
        "",
        f"**Summary:** {analysis.summary}",
        "",
        "## Incident",
        f"- Error: `{inc.error_type_full}`: {first_line}",
        f"- Request: {inc.request or 'unknown'}",
        f"- Location: `{inc.file}:{inc.line}` in `{inc.function}()`",
        f"- First seen: {inc.timestamp:%Y-%m-%d %H:%M:%S} | occurrences: {inc.frequency}",
        f"- Category: {cls.category or 'unclassified'} ({cls.source})",
        f"- Deployed version: `{state.get('ref')}`",
        "",
        "## Probable root causes (ranked)",
    ]
    for n, h in enumerate(analysis.hypotheses, 1):
        lines += [
            f"### {n}. {h.cause}",
            f"- Confidence (the model's rough estimate, not a calibrated probability): {h.confidence}%",
            f"- Suggested fix: {h.suggested_fix}",
            f"- Risk of applying the fix: {h.risk_level}",
        ]
        if h.files_involved:
            lines.append(f"- Files: {', '.join(f'`{f}`' for f in h.files_involved)}")
        lines.append("- Evidence:")
        lines += [f"  - {e}" for e in h.evidence]
        lines += [f"- Quoted evidence found in the source material: {h.quotes_verified}/{h.quotes_total}", ""]
    patch, validation = state.get("patch"), state.get("validation")
    if patch:
        if validation is None:
            badge = "NOT VALIDATED (sandbox unavailable)"
        elif validation.accepted:
            badge = "VALIDATED in the sandbox"
        elif validation.passed:
            badge = "NEEDS HUMAN REVIEW (passed the sandbox, but the review found problems)"
        else:
            badge = "REJECTED by the sandbox"
        lines += [f"## Proposed patch: {badge}", patch.explanation, "", "```diff", patch.diff.rstrip(), "```", ""]
        if validation:
            b, a = validation.baseline, validation.patched
            lines += [
                f"- Sandbox ({validation.mode}): {validation.reason}",
                f"- Bug reproduced on the original code: {'yes' if validation.bug_reproduced else 'no'}",
                f"- Test suite before -> after: {b.tests_passed} passed / {b.tests_failed} failed"
                f" -> {a.tests_passed} passed / {a.tests_failed} failed",
                f"- Replay of the failing request, before -> after: {b.replay_statuses} -> {a.replay_statuses}",
                f"- Patch attempts: {state.get('patch_attempt')}",
            ]
            lines += [f"- REVIEW WARNING: {w}" for w in validation.warnings]
            lines += ["- The sandbox shows the patch stops the crash and breaks no test. It cannot prove the "
                      "business behaviour is right: a human must review it before merging.", ""]
        elif state.get("sandbox_error"):
            lines += [f"- Sandbox error: {state['sandbox_error']}", ""]
    elif state.get("patch_error"):
        lines += ["## Proposed patch", f"No usable patch was produced: {state['patch_error']}", ""]
    lines.append("## Investigation timeline")
    lines += [f"- `{t['at']}` **{t['step']}**: {t['detail']}" for t in state.get("timeline", [])]
    lines += ["", "> Generated automatically by a prototype. Review before acting on it."]
    return "\n".join(lines)
