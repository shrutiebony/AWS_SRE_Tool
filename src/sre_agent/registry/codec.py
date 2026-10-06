"""Encode registry records as JSON-safe documents."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Any

from sre_agent.models import (
    ActionRecord,
    Diagnosis,
    Escalation,
    Evidence,
    HealthAssessment,
    Lesson,
    Outcome,
    PolicyDecision,
    ProposedAction,
    Relationship,
    ResourceRecord,
    ScanAccess,
    VerificationResult,
)

TYPES: dict[str, type] = {
    "ResourceRecord": ResourceRecord,
    "Relationship": Relationship,
    "ScanAccess": ScanAccess,
    "Evidence": Evidence,
    "ProposedAction": ProposedAction,
    "Diagnosis": Diagnosis,
    "PolicyDecision": PolicyDecision,
    "ActionRecord": ActionRecord,
    "VerificationResult": VerificationResult,
    "Escalation": Escalation,
    "Outcome": Outcome,
    "Lesson": Lesson,
    "HealthAssessment": HealthAssessment,
}


def to_plain(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        payload = {"__type__": type(obj).__name__}
        for item in fields(obj):
            payload[item.name] = to_plain(getattr(obj, item.name))
        return payload
    if isinstance(obj, dict):
        return {str(key): to_plain(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(value) for value in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def from_plain(obj: Any) -> Any:
    if isinstance(obj, list):
        return [from_plain(value) for value in obj]
    if isinstance(obj, dict):
        if "__type__" in obj:
            cls = TYPES[obj["__type__"]]
            payload = {key: from_plain(value) for key, value in obj.items() if key != "__type__"}
            return cls(**payload)
        return {key: from_plain(value) for key, value in obj.items()}
    return obj
