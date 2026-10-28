"""Log Parser: turns raw application logs into structured Incident objects.

Rules first (regex). No LLM is needed for well-formed Python tracebacks.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

from app.models.incident import Incident, StackFrame

# Frames whose path contains this marker are "our" code, not library code.
PROJECT_MARKER = "demo_app/"

LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \| (\w+) \| ([\w.]+) \| (.*)$"
)
FRAME = re.compile(r'^\s+File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$')
EXCEPTION = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s?(?P<msg>.*))?$")
REQUEST = re.compile(r"\b(?P<method>GET|POST|PUT|PATCH|DELETE) (?P<path>/\S*)")
TRACEBACK_START = "Traceback (most recent call last):"


def relative_path(path: str) -> str:
    """/private/tmp/xyz/repo/demo_app/shop/a.py -> demo_app/shop/a.py"""
    idx = path.rfind(PROJECT_MARKER)
    return path[idx:] if idx != -1 else path


def split_entries(text: str) -> list[dict]:
    """Group raw lines into log entries. Lines that do not start with a
    timestamp (traceback lines) belong to the previous entry."""
    entries: list[dict] = []
    for raw in text.splitlines():
        m = LOG_LINE.match(raw)
        if m:
            ts, level, logger_name, message = m.groups()
            entries.append({
                "timestamp": datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f"),
                "level": level,
                "logger": logger_name,
                "message": message,
                "body": [],
            })
        elif entries:
            entries[-1]["body"].append(raw)
    return entries


def _is_caret_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) <= set("^~")


def parse_traceback(body: list[str]):
    """Return (frames, exception_type, message) for the LAST traceback in the
    body, or None. The last one matters: with chained exceptions it holds the
    exception that finally reached the top."""
    starts = [i for i, line in enumerate(body) if line.strip() == TRACEBACK_START]
    if not starts:
        return None
    block = body[starts[-1] + 1:]

    frames: list[StackFrame] = []
    last_frame_idx = -1
    for i, line in enumerate(block):
        m = FRAME.match(line)
        if not m:
            continue
        last_frame_idx = i
        code = None
        if i + 1 < len(block):
            nxt = block[i + 1]
            if nxt.startswith("    ") and not FRAME.match(nxt) and not _is_caret_line(nxt):
                code = nxt.strip()
        path = m.group("file")
        is_project = PROJECT_MARKER in path
        frames.append(StackFrame(
            file=relative_path(path) if is_project else path,
            line=int(m.group("line")),
            function=m.group("func"),
            code=code,
            is_project_code=is_project,
        ))

    exc_type, exc_msg = "UnknownError", ""
    for j in range(last_frame_idx + 1, len(block)):
        line = block[j]
        if line and not line[0].isspace():
            m = EXCEPTION.match(line)
            if m:
                exc_type = m.group("type")
                exc_msg = "\n".join([m.group("msg") or ""] + block[j + 1:]).strip()
            break
    return frames, exc_type, exc_msg


def build_incident(entry: dict, previous: list[dict]):
    parsed = parse_traceback(entry["body"])
    if parsed is None:
        return None
    frames, exc_full, exc_msg = parsed

    # The culprit is the LAST frame inside project code (not library code).
    project_frames = [f for f in frames if f.is_project_code]
    culprit = project_frames[-1] if project_frames else (frames[-1] if frames else None)

    req = REQUEST.search(entry["message"])
    return Incident(
        error_type=exc_full.split(".")[-1],
        error_type_full=exc_full,
        message=exc_msg,
        timestamp=entry["timestamp"],
        last_seen=entry["timestamp"],
        logger=entry["logger"],
        service=entry["logger"].split(".")[0],
        request=f"{req['method']} {req['path']}" if req else None,
        file=culprit.file if culprit else None,
        function=culprit.function if culprit else None,
        line=culprit.line if culprit else None,
        code_line=culprit.code if culprit else None,
        frames=frames,
        stack_trace="\n".join(entry["body"]).strip(),
        context_logs=[f"{e['level']} | {e['logger']} | {e['message']}" for e in previous],
    )


def parse_log_text(text: str) -> list[Incident]:
    entries = split_entries(text)
    incidents: dict[tuple, Incident] = {}
    for i, entry in enumerate(entries):
        if entry["level"] not in ("ERROR", "CRITICAL"):
            continue
        inc = build_incident(entry, entries[max(0, i - 5):i])
        if inc is None:
            continue
        key = (inc.error_type_full, inc.file, inc.function, inc.line)
        if key in incidents:  # same error again -> count it, don't duplicate
            incidents[key].frequency += 1
            incidents[key].last_seen = inc.timestamp
        else:
            incidents[key] = inc
    return list(incidents.values())


def parse_log_file(path) -> list[Incident]:
    return parse_log_text(Path(path).read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    for incident in parse_log_file(sys.argv[1]):
        print(incident.model_dump_json(indent=2, exclude={"frames", "stack_trace"}))
