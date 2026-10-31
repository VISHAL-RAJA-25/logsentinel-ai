"""Root Cause + Recommendation agent.

Sends the incident and the Repo Explorer's context to an LLM and requires
structured JSON back: ranked hypotheses with evidence, a fix and a risk level.
The reply is validated with Pydantic, and the evidence the model quotes is
checked against the material it was given.
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from app.agents.classifier import Classification, classify
from app.agents.log_parser import parse_log_file
from app.agents.repo_explorer import explore, format_context
from app.models.analysis import Analysis
from app.models.incident import Incident

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")

SYSTEM_PROMPT = """You are an expert Site Reliability Engineer investigating a production incident in a Python/FastAPI service.
You receive a structured incident, a category hint, the failing code, and recent git commits with diffs.

Rules:
- Base every claim on the material provided. Never invent files, functions, commits or log lines.
- Read the recent commit diffs carefully: the cause is often a change in a file that is NOT in the stack trace.
- Give 1 to 3 hypotheses, most likely first.
- evidence: 1 to 4 short items. Each item must contain at least one exact snippet copied from the material, wrapped in backticks.
- confidence: an integer from 0 to 100 for how likely the hypothesis is the true root cause. Alternative hypotheses should not all be high.
- suggested_fix: one concrete action. Name the file and say what to change.
- risk_level: "low", "medium" or "high" - the risk of applying that fix.
- Reply with a single JSON object only. No markdown fences and no text outside the JSON.

JSON format:
{
  "summary": "<one sentence: what happened>",
  "hypotheses": [
    {
      "cause": "<root cause in one or two sentences>",
      "confidence": 0,
      "evidence": ["<evidence containing an exact `snippet`>"],
      "suggested_fix": "<concrete fix>",
      "risk_level": "low",
      "files_involved": ["demo_app/shop/example.py"]
    }
  ]
}"""


class AnalysisError(Exception):
    pass


def default_llm(messages: list[dict]) -> str:
    from litellm import completion  # imported here so offline tests need no key

    resp = completion(
        model=os.getenv("LLM_MODEL"),
        messages=messages,
        temperature=0,
        max_tokens=3000,
        num_retries=3,  # retries rate-limit errors with backoff
    )
    return resp.choices[0].message.content or ""


def extract_json(text: str) -> dict:
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the reply")
    return json.loads(text[start:end + 1])


def build_user_prompt(incident: Incident, classification: Classification, context_text: str) -> str:
    logs = "\n".join(f"- {line}" for line in incident.context_logs) or "- (none)"
    return (
        "INCIDENT\n"
        f"Error: {incident.error_type_full}: {incident.message[:600]}\n"
        f"Request: {incident.request}\n"
        f"Category hint: {classification.category or 'unknown'}\n"
        f"Occurrences: {incident.frequency}\n"
        f"Log lines before the error:\n{logs}\n\n"
        f"CODE AND HISTORY\n{context_text}"
    )


def _norm(text: str) -> str:
    return " ".join(text.split())


def check_grounding(analysis: Analysis, source_text: str) -> Analysis:
    """Count how many backtick-quoted snippets really appear in the material
    the model was given. A cheap check against made-up evidence."""
    haystack = _norm(source_text)
    for h in analysis.hypotheses:
        quotes = [q for item in h.evidence for q in re.findall(r"`([^`]+)`", item)]
        h.quotes_total = len(quotes)
        h.quotes_verified = sum(1 for q in quotes if _norm(q) in haystack)
    return analysis


def analyze(incident: Incident, classification: Classification, context_text: str,
            llm: Callable[[list[dict]], str] = default_llm, max_attempts: int = 2) -> Analysis:
    user_prompt = build_user_prompt(incident, classification, context_text)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}]
    last_error = None
    for _ in range(max_attempts):
        raw = llm(messages)
        try:
            data = extract_json(raw)
            data["hypotheses"] = data.get("hypotheses", [])[:3]
            analysis = Analysis.model_validate(data)
            break
        except (ValueError, TypeError, AttributeError) as exc:  # includes ValidationError
            last_error = exc
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Your reply was invalid: {str(exc)[:400]}\n"
                                            "Reply again with ONLY the corrected JSON object."},
            ]
    else:
        raise AnalysisError(f"No valid JSON after {max_attempts} attempts: {last_error}")
    analysis.hypotheses.sort(key=lambda h: h.confidence, reverse=True)
    return check_grounding(analysis, user_prompt)


def investigate_dataset(folder: Path, llm: Callable[[list[dict]], str] = default_llm):
    """Parse -> classify -> explore -> analyze for one dataset folder."""
    truth = json.loads((folder / "ground_truth.json").read_text())
    incident = parse_log_file(folder / "app.log")[0]
    classification = classify(incident)
    context_text = format_context(explore(incident, ref=truth["branch"], repo_path=ROOT))
    analysis = analyze(incident, classification, context_text, llm)
    return truth, classification, analysis


def print_summary(truth: dict, classification: Classification, analysis: Analysis) -> None:
    print(f"  expected : {truth['root_cause']}")
    print(f"  category : {classification.category} ({classification.source})")
    for n, h in enumerate(analysis.hypotheses, 1):
        print(f"  #{n} [{h.confidence}%] {h.cause}")
        print(f"      fix: {h.suggested_fix} (risk: {h.risk_level}, "
              f"quotes verified {h.quotes_verified}/{h.quotes_total})")


if __name__ == "__main__":
    datasets = ROOT / "datasets"
    out_dir = ROOT / "results" / "step6"
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = sys.argv[1:]  # e.g. 001 004 ; no arguments = all datasets
    folders = [datasets / w for w in wanted] or sorted(d for d in datasets.iterdir() if d.is_dir())
    for folder in folders:
        print(f"\nIncident {folder.name}")
        try:
            truth, classification, analysis = investigate_dataset(folder)
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        print_summary(truth, classification, analysis)
        (out_dir / f"{folder.name}.json").write_text(json.dumps({
            "id": folder.name,
            "model": os.getenv("LLM_MODEL"),
            "classification": classification.model_dump(),
            "analysis": analysis.model_dump(),
        }, indent=2))
        time.sleep(3)  # stay under free-tier rate limits
