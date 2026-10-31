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
    lines.append("## Investigation timeline")
    lines += [f"- `{t['at']}` **{t['step']}**: {t['detail']}" for t in state.get("timeline", [])]
    lines += ["", "> Generated automatically by a prototype. Review before acting on it."]
    return "\n".join(lines)
