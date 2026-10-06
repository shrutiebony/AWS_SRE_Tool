"""Evidence required before a repair can be recommended or executed."""

from __future__ import annotations

from sre_agent.models import Diagnosis


def validate_evidence(diagnosis: Diagnosis) -> list[str]:
    errors: list[str] = []
    if not diagnosis.observed_problem.strip():
        errors.append("The diagnosis does not state the observed problem.")
    known = {item.id for item in diagnosis.evidence}
    if diagnosis.health != "unknown" and diagnosis.problem_code != "presence.uncertain" and not diagnosis.evidence:
        errors.append("The diagnosis has no evidence.")
    action = diagnosis.proposed_action
    if diagnosis.health == "unknown" and action is not None:
        errors.append("Unknown health cannot propose a repair.")
    if action is not None:
        if not action.evidence_ids:
            errors.append("The proposed action does not cite evidence.")
        if not set(action.evidence_ids) <= known:
            errors.append("The proposed action cites evidence the diagnosis does not contain.")
        if not action.rationale.strip():
            errors.append("The proposed action has no rationale.")
    return errors
