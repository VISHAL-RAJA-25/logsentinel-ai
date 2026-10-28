from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class StackFrame(BaseModel):
    file: str
    line: int
    function: str
    code: Optional[str] = None
    is_project_code: bool = False


class Incident(BaseModel):
    """One structured incident extracted from raw logs."""

    error_type: str = Field(description="Short name, e.g. AttributeError")
    error_type_full: str = Field(description="Dotted name, e.g. sqlalchemy.exc.IntegrityError")
    message: str
    timestamp: datetime
    last_seen: datetime
    logger: str
    service: str
    request: Optional[str] = None
    file: Optional[str] = None
    function: Optional[str] = None
    line: Optional[int] = None
    code_line: Optional[str] = None
    frames: list[StackFrame] = Field(default_factory=list)
    stack_trace: str
    context_logs: list[str] = Field(default_factory=list)
    frequency: int = 1
