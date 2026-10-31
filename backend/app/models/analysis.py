from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Hypothesis(BaseModel):
    cause: str
    confidence: int = Field(ge=0, le=100, description="Rough signal from the model, not a calibrated probability")
    evidence: list[str] = Field(min_length=1)
    suggested_fix: str
    risk_level: Literal["low", "medium", "high"]
    files_involved: list[str] = Field(default_factory=list)
    # filled in by our own grounding check, not by the LLM
    quotes_total: int = 0
    quotes_verified: int = 0

    @field_validator("confidence", mode="before")
    @classmethod
    def _scale_confidence(cls, v):
        if isinstance(v, str):
            v = v.strip().rstrip("%")
        v = float(v)
        if 0 < v <= 1:  # model answered 0.9 instead of 90
            v *= 100
        return round(v)

    @field_validator("risk_level", mode="before")
    @classmethod
    def _lower_risk(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class Analysis(BaseModel):
    summary: str
    hypotheses: list[Hypothesis] = Field(min_length=1)
