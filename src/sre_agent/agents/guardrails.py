"""Reject model text that is not grounded in a rule diagnosis."""

from __future__ import annotations

import re

from sre_agent.models import Diagnosis

_IDENTIFIER = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\b")
_ARN = re.compile(r"arn:aws:[A-Za-z0-9:_\-./]+")
_ALLOWED_FIELDS = frozenset({"summary", "evidence_ids", "resource_id", "problem_code", "action"})


class GuardrailRejection(Exception):
    """The model added a claim the diagnosis does not support."""


def evidence_packet(diagnosis: Diagnosis) -> dict:
    """The only facts a narrator is allowed to see."""
    action = diagnosis.proposed_action
    return {
        "resource_id": diagnosis.resource_id,
        "problem_code": diagnosis.problem_code,
        "observed_problem": diagnosis.observed_problem,
        "health": diagnosis.health,
        "action": action.runbook_id if action else "none",
        "evidence": [
            {"id": item.id, "source": item.source, "summary": item.summary}
            for item in diagnosis.evidence
        ],
    }


def decision_fingerprint(diagnosis: Diagnosis) -> tuple:
    action = diagnosis.proposed_action
    return (
        diagnosis.incident_id,
        diagnosis.resource_id,
        diagnosis.problem_code,
        diagnosis.status,
        action.runbook_id if action else None,
        action.target_resource_id if action else None,
        action.automatic_eligible if action else None,
        tuple(action.evidence_ids) if action else (),
    )


def accept_explanation(diagnosis: Diagnosis, payload: dict) -> str:
    """Return a summary only when every claim matches the rule diagnosis."""
    if not isinstance(payload, dict):
        raise GuardrailRejection("The model response was not an object.")
    extra = set(payload) - _ALLOWED_FIELDS
    if extra:
        raise GuardrailRejection("The model returned fields it is not allowed to set.")
    packet = evidence_packet(diagnosis)
    if payload.get("resource_id") != packet["resource_id"]:
        raise GuardrailRejection("The model named a different resource.")
    if payload.get("problem_code") != packet["problem_code"]:
        raise GuardrailRejection("The model changed the problem code.")
    if payload.get("action") != packet["action"]:
        raise GuardrailRejection("The model changed the action.")
    ids = payload.get("evidence_ids")
    known = {item["id"] for item in packet["evidence"]}
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise GuardrailRejection("The model evidence list was not a list of ids.")
    if set(ids) - known:
        raise GuardrailRejection("The model cited evidence that was not observed.")
    if known and not ids:
        raise GuardrailRejection("The model did not cite observed evidence.")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise GuardrailRejection("The model summary was empty.")
    summary = summary.strip()
    if len(summary) > 500:
        raise GuardrailRejection("The model summary was too long.")
    allowed_names = {packet["problem_code"]}
    if packet["action"] != "none":
        allowed_names.add(packet["action"])
    for name in _IDENTIFIER.findall(summary):
        if name not in allowed_names:
            raise GuardrailRejection("The model named an action or problem that was not diagnosed.")
    known_arns = _arns(packet)
    for arn in _ARN.findall(summary):
        if arn not in known_arns:
            raise GuardrailRejection("The model named a resource that was not observed.")
    return summary


def _arns(packet: dict) -> set[str]:
    found = set(_ARN.findall(packet["resource_id"]))
    found.update(_ARN.findall(packet["observed_problem"]))
    for item in packet["evidence"]:
        found.update(_ARN.findall(item["summary"]))
    return found
