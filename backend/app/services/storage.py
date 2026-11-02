"""Stores every investigation so the dashboard (and our evaluation) can read it back.

SQLite by default. Set LOGSENTINEL_DB to a PostgreSQL URL later - nothing else changes.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_URL = f"sqlite:///{ROOT / 'logsentinel.db'}"
Base = declarative_base()

# columns added after the first version of the table (added automatically to older databases)
NEW_COLUMNS = {"patch_status": "VARCHAR", "patch_attempts": "INTEGER",
               "patch_diff": "TEXT", "validation_json": "TEXT"}


class InvestigationRow(Base):
    __tablename__ = "investigations"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    status = Column(String, nullable=False)  # completed | no_incident | failed
    log_path = Column(String)
    ref = Column(String)
    error_type = Column(String)
    category = Column(String)
    top_cause = Column(Text)
    top_confidence = Column(Integer)
    attempts = Column(Integer, default=0)
    duration_s = Column(Float)
    error = Column(Text)
    incident_json = Column(Text)
    analysis_json = Column(Text)
    report_md = Column(Text)
    timeline_json = Column(Text)
    patch_status = Column(String)  # none | no_patch | unvalidated | rejected | validated
    patch_attempts = Column(Integer)
    patch_diff = Column(Text)
    validation_json = Column(Text)


def _to_dict(row: InvestigationRow) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "status": row.status,
        "log_path": row.log_path,
        "ref": row.ref,
        "error_type": row.error_type,
        "category": row.category,
        "top_cause": row.top_cause,
        "top_confidence": row.top_confidence,
        "attempts": row.attempts,
        "duration_s": row.duration_s,
        "error": row.error,
        "incident": json.loads(row.incident_json) if row.incident_json else None,
        "analysis": json.loads(row.analysis_json) if row.analysis_json else None,
        "report_md": row.report_md,
        "timeline": json.loads(row.timeline_json) if row.timeline_json else [],
        "patch_status": row.patch_status,
        "patch_attempts": row.patch_attempts,
        "patch_diff": row.patch_diff,
        "validation": json.loads(row.validation_json) if row.validation_json else None,
    }


def _patch_status(state: dict) -> str:
    patch, validation = state.get("patch"), state.get("validation")
    if patch is None:
        return "no_patch" if state.get("patch_error") else "none"
    if validation is None:
        return "unvalidated"
    if validation.accepted:
        return "validated"
    return "needs_review" if validation.passed else "rejected"


class Store:
    def __init__(self, url: Optional[str] = None):
        self.engine = create_engine(url or os.getenv("LOGSENTINEL_DB", DEFAULT_URL))
        Base.metadata.create_all(self.engine)
        self._migrate()
        self._session = sessionmaker(self.engine)

    def _migrate(self) -> None:
        existing = {c["name"] for c in inspect(self.engine).get_columns("investigations")}
        with self.engine.begin() as conn:
            for name, sql_type in NEW_COLUMNS.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE investigations ADD COLUMN {name} {sql_type}"))

    def save(self, state: dict) -> int:
        incident, analysis = state.get("incident"), state.get("analysis")
        classification = state.get("classification")
        top = analysis.hypotheses[0] if analysis else None
        patch, validation = state.get("patch"), state.get("validation")
        row = InvestigationRow(
            status=state.get("status", "unknown"),
            log_path=state.get("log_path"),
            ref=state.get("ref"),
            error_type=incident.error_type if incident else None,
            category=classification.category if classification else None,
            top_cause=top.cause if top else None,
            top_confidence=top.confidence if top else None,
            attempts=state.get("attempt", 0),
            duration_s=state.get("duration_s"),
            error=state.get("error"),
            incident_json=incident.model_dump_json() if incident else None,
            analysis_json=analysis.model_dump_json() if analysis else None,
            report_md=state.get("report"),
            timeline_json=json.dumps(state.get("timeline", [])),
            patch_status=_patch_status(state),
            patch_attempts=state.get("patch_attempt", 0),
            patch_diff=patch.diff if patch else None,
            validation_json=json.dumps({
                "final": validation.model_dump() if validation else None,
                "history": state.get("patch_history", []),
                "sandbox_error": state.get("sandbox_error"),
            }) if (validation or state.get("patch_history") or state.get("sandbox_error")) else None,
        )
        with self._session() as session:
            session.add(row)
            session.commit()
            return row.id

    def get(self, investigation_id: int) -> Optional[dict]:
        with self._session() as session:
            row = session.get(InvestigationRow, investigation_id)
            return _to_dict(row) if row else None

    def list(self, limit: int = 50) -> list[dict]:
        with self._session() as session:
            rows = (session.query(InvestigationRow)
                    .order_by(InvestigationRow.id.desc()).limit(limit).all())
            return [_to_dict(r) for r in rows]
