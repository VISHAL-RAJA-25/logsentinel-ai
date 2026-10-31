from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CommitInfo(BaseModel):
    sha: str
    author: str
    date: datetime
    message: str
    files: list[str] = Field(default_factory=list)
    diff: str = ""


class BlameInfo(BaseModel):
    sha: str
    author: str
    date: datetime
    summary: str


class RepoContext(BaseModel):
    """Only the code and history relevant to one incident."""

    ref: str
    head_sha: str
    culprit_file: Optional[str] = None
    culprit_function: Optional[str] = None
    failing_line: Optional[int] = None
    function_source: Optional[str] = None
    imports: list[str] = Field(default_factory=list)
    module_constants: list[str] = Field(default_factory=list)
    call_chain: list[str] = Field(default_factory=list)
    blame: Optional[BlameInfo] = None
    recent_commits: list[CommitInfo] = Field(default_factory=list)
