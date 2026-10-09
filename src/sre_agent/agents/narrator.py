"""Explain a diagnosis without being allowed to change it.

Without GEMINI_API_KEY the narrator quotes the rule diagnosis. With a key,
Gemini may restate that packet. A response that adds a resource, a problem,
or an action is discarded.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

from sre_agent.agents.guardrails import (
    GuardrailRejection,
    accept_explanation,
    decision_fingerprint,
    evidence_packet,
)
from sre_agent.models import Diagnosis


@dataclass(frozen=True)
class Narration:
    incident_id: str
    text: str
    source: str
    rejected: str = ""


def explain(diagnosis: Diagnosis, complete=None) -> Narration:
    before = decision_fingerprint(diagnosis)
    if complete is None:
        complete = _gemini_complete if os.environ.get("GEMINI_API_KEY", "").strip() else None
    if complete is None:
        return Narration(diagnosis.incident_id, _from_rules(diagnosis), "rules")
    rejected = ""
    try:
        raw = complete(_prompt(evidence_packet(diagnosis)))
        payload = json.loads(_strip_fence(raw))
        text = accept_explanation(diagnosis, payload)
        source = "gemini"
    except (GuardrailRejection, json.JSONDecodeError, ValueError, URLError, TimeoutError, OSError) as exc:
        text = _from_rules(diagnosis)
        source = "rules"
        rejected = str(exc)
    if decision_fingerprint(diagnosis) != before:
        _restore(diagnosis, before)
        text = _from_rules(diagnosis)
        source = "rules"
        rejected = rejected or "The model changed the diagnosis and the change was reverted."
    return Narration(diagnosis.incident_id, text, source, rejected)


def _from_rules(diagnosis: Diagnosis) -> str:
    packet = evidence_packet(diagnosis)
    cited = ", ".join(item["id"] for item in packet["evidence"]) or "none"
    return (
        f"{packet['observed_problem']} "
        f"Problem {packet['problem_code']} on {packet['resource_id']}. "
        f"Evidence {cited}. Proposed action {packet['action']}."
    )


def _prompt(packet: dict) -> str:
    body = json.dumps(packet, sort_keys=True)
    return (
        "You explain an SRE diagnosis. Use only the JSON packet. "
        "Do not invent resources, metrics, causes, or actions. "
        "Return one JSON object with keys summary, evidence_ids, resource_id, problem_code, and action. "
        "Copy resource_id, problem_code, and action exactly. "
        "evidence_ids must be ids from the packet. "
        "summary must be one sentence under 500 characters and may name only the packet action and problem code.\n"
        f"{body}"
    )


def _strip_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


def _gemini_complete(prompt: str) -> str:
    key = os.environ["GEMINI_API_KEY"].strip()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    request = Request(
        url,
        data=json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 512,
                    "responseMimeType": "application/json",
                },
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        body = json.loads(response.read().decode())
    parts = body["candidates"][0]["content"]["parts"]
    return str(parts[0]["text"])


def _restore(diagnosis: Diagnosis, before: tuple) -> None:
    from sre_agent.models import ProposedAction

    diagnosis.incident_id = before[0]
    diagnosis.resource_id = before[1]
    diagnosis.problem_code = before[2]
    diagnosis.status = before[3]
    runbook_id, target, eligible, evidence_ids = before[4], before[5], before[6], before[7]
    if runbook_id is None:
        diagnosis.proposed_action = None
        return
    current = diagnosis.proposed_action
    rationale = current.rationale if current and current.runbook_id == runbook_id else "restored after a rejected model edit"
    diagnosis.proposed_action = ProposedAction(
        runbook_id, target or diagnosis.resource_id, rationale, list(evidence_ids), bool(eligible)
    )
