"""Classifier: decides what KIND of problem an incident is.

Ordered keyword rules first. If no rule is confident, the rules abstain
(they never guess) and an LLM is asked as a fallback.
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel

from app.agents.log_parser import parse_log_file
from app.models.incident import Incident

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

CATEGORIES = [
    "Authentication", "Database", "Network", "Memory", "CPU", "Deployment",
    "Configuration", "Third-party API", "Permission", "Security", "Caching",
    "Timeout", "Application Logic",
]


class Classification(BaseModel):
    category: Optional[str]
    source: str  # "rules" | "llm" | "unclassified"
    reason: str


CONSTANT = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")  # e.g. MAX_PAYMENT_AMOUNT
CONFIG_CODE = re.compile(r"os\.environ|getenv")
DB_CODE = re.compile(r"\b\w*(?:db|session)\w*\.(?:query|execute|commit|add|flush|refresh|rollback)\(")
AUTH_HINT = re.compile(r"auth|login|password|credential|token", re.IGNORECASE)


def _config_signal(i: Incident) -> bool:
    code = i.code_line or ""
    return bool(CONFIG_CODE.search(code) or CONSTANT.search(code) or CONSTANT.search(i.message))


def _auth_hint(i: Incident) -> bool:
    return bool(AUTH_HINT.search(f"{i.function or ''} {i.code_line or ''}"))


# (rule name, category, test) - the first rule that matches wins
RULES = [
    ("missing module", "Deployment",
     lambda i: i.error_type in {"ModuleNotFoundError", "ImportError"}),
    ("permission denied", "Permission",
     lambda i: i.error_type == "PermissionError"),
    ("timeout", "Timeout",
     lambda i: i.error_type in {"TimeoutError", "ReadTimeout", "ConnectTimeout"}
     or bool(re.search(r"timed out|timeout", i.message, re.IGNORECASE))),
    ("HTTP client library error", "Third-party API",
     lambda i: i.error_type_full.startswith(("httpx.", "requests.", "urllib3."))),
    ("connection error", "Network",
     lambda i: i.error_type in {"ConnectionError", "ConnectionRefusedError", "ConnectionResetError"}),
    ("database library error", "Database",
     lambda i: i.error_type_full.startswith(("sqlalchemy.", "sqlite3.", "psycopg", "pymysql.", "asyncpg."))),
    ("environment variable or config constant involved", "Configuration", _config_signal),
    ("out of memory", "Memory",
     lambda i: i.error_type == "MemoryError"),
    ("plain code error", "Application Logic",
     lambda i: i.error_type in {"RecursionError", "IndexError", "ZeroDivisionError"}),
    ("database call on the failing line", "Database",
     lambda i: bool(DB_CODE.search(i.code_line or ""))),
    ("authentication code involved", "Authentication", _auth_hint),
]


def classify_with_rules(incident: Incident) -> Classification:
    for name, category, test in RULES:
        if test(incident):
            return Classification(category=category, source="rules", reason=name)
    return Classification(category=None, source="unclassified", reason="no rule matched")


def classify_with_llm(incident: Incident) -> Classification:
    from litellm import completion  # imported here so offline tests need no key

    prompt = (
        "Classify this production incident into exactly one category.\n"
        f"Categories: {', '.join(CATEGORIES)}\n\n"
        f"Error: {incident.error_type_full}: {incident.message[:500]}\n"
        f"Request: {incident.request}\n"
        f"Location: {incident.file} in {incident.function}() line {incident.line}\n"
        f"Failing code: {incident.code_line}\n"
        f"Earlier log lines: {incident.context_logs}\n\n"
        'Reply with JSON only: {"category": "<one category>", "reason": "<one sentence>"}'
    )
    try:
        resp = completion(
            model=os.getenv("LLM_MODEL"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        text = resp.choices[0].message.content
        data = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))
        wanted = str(data.get("category", "")).strip().lower()
        for category in CATEGORIES:
            if category.lower() == wanted:
                return Classification(category=category, source="llm",
                                      reason=str(data.get("reason", ""))[:300])
        return Classification(category=None, source="unclassified",
                              reason=f"LLM returned unknown category: {wanted!r}")
    except Exception as exc:  # network error, bad JSON, rate limit ...
        return Classification(category=None, source="unclassified",
                              reason=f"LLM fallback failed: {type(exc).__name__}")


def classify(incident: Incident, use_llm: bool = True) -> Classification:
    result = classify_with_rules(incident)
    if result.category is None and use_llm:
        return classify_with_llm(incident)
    return result


if __name__ == "__main__":
    for inc in parse_log_file(sys.argv[1]):
        print(inc.error_type_full, "->", classify(inc).model_dump_json())
