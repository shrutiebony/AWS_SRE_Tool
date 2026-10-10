"""Plain-language plans for demo actions. Building a plan does not call AWS."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_UNCHANGED = "Leave DynamoDB table enterprise-sites and every sre-agent-* resource unchanged in {region}."

_SPECS: dict[str, dict] = {
    "add": {
        "title": "Add a table",
        "requires_verify": True,
        "simulation": False,
        "button": "Verify and apply",
        "lead": "Nothing in AWS changes until you press Verify and apply.",
        "items": [
            "Create DynamoDB table demo-extra-sites in {region}.",
            "Run inventory so the checker can see it.",
            _UNCHANGED,
        ],
    },
    "remove": {
        "title": "Delete the demo table",
        "requires_verify": True,
        "simulation": False,
        "button": "Verify and apply",
        "lead": "Nothing in AWS changes until you press Verify and apply.",
        "items": [
            "Delete DynamoDB table demo-extra-sites in {region}.",
            "Run inventory so the checker can see it is gone.",
            _UNCHANGED,
        ],
    },
    "break": {
        "title": "Break the directory",
        "requires_verify": True,
        "simulation": False,
        "button": "Verify and apply",
        "lead": "Nothing in AWS changes until you press Verify and apply.",
        "items": [
            "Set Lambda function enterprise-sites-directory TABLE_NAME to missing-table in {region}.",
            "Call the sites URL so the failure shows up.",
            "Wait for the Lambda Errors metric.",
            "Run inventory so the checker can see the failure.",
            _UNCHANGED,
        ],
    },
    "restore": {
        "title": "Restore the directory",
        "requires_verify": True,
        "simulation": False,
        "button": "Verify and apply",
        "lead": "Nothing in AWS changes until you press Verify and apply.",
        "items": [
            "Set Lambda function enterprise-sites-directory TABLE_NAME to enterprise-sites in {region}.",
            "Call the sites URL.",
            "Wait for a clean Lambda metric period.",
            "Run inventory, then close the directory Jira ticket because this verified action was performed.",
            _UNCHANGED,
        ],
    },
    "topic": {
        "title": "Create the topic",
        "requires_verify": True,
        "simulation": False,
        "button": "Verify and apply",
        "lead": "Nothing in AWS changes until you press Verify and apply.",
        "items": [
            "Create SNS topic demo-alerts in {region}.",
            "Tag the topic so the checker can see it.",
            "Run inventory so the checker can see it.",
            _UNCHANGED,
        ],
    },
    "delete-topic": {
        "title": "Delete the topic",
        "requires_verify": True,
        "simulation": False,
        "button": "Verify and apply",
        "lead": "Nothing in AWS changes until you press Verify and apply.",
        "items": [
            "Delete SNS topic demo-alerts in {region}.",
            "Run inventory so the checker can see it is gone.",
            _UNCHANGED,
        ],
    },
    "reset": {
        "title": "Clean the demo resources",
        "requires_verify": True,
        "simulation": False,
        "button": "Verify and apply",
        "lead": "Nothing in AWS changes until you press Verify and apply.",
        "items": [
            "Set Lambda function enterprise-sites-directory TABLE_NAME to enterprise-sites in {region}.",
            "Delete DynamoDB table demo-extra-sites in {region} if it exists.",
            "Delete SNS topic demo-alerts in {region} if it exists.",
            _UNCHANGED,
        ],
    },
    "close": {
        "title": "Simulated close",
        "requires_verify": False,
        "simulation": True,
        "button": "Run simulation",
        "lead": "This is a simulation. It does not call AWS and does not change the live workload.",
        "items": [
            "Read the simulated dead-letter queue as unhealthy.",
            "Propose sqs.redrive_dlq and do not run that repair on the live account.",
            "Replay only the simulated transient failure.",
            "Count recovery only after simulated telemetry, not when an API call returns.",
            "Move the simulated task to Done. A live ticket closes only after its own verified action.",
        ],
    },
}

ALIASES = {
    "add": "add",
    "remove": "remove",
    "break": "break",
    "restore": "restore",
    "topic": "topic",
    "delete-topic": "delete-topic",
    "reset": "reset",
    "close": "close",
    "demo1_create": "add",
    "demo1_delete": "remove",
    "demo2_break": "break",
    "demo2_restore": "restore",
    "demo2_lab": "close",
    "demo3_create": "topic",
    "demo3_delete": "delete-topic",
}


def plan_for(action: str, region: str) -> dict | None:
    key = ALIASES.get(action)
    if key is None:
        return None
    spec = _SPECS[key]
    place = region.strip() or "the configured region"
    items = [f"{index}. {line.format(region=place)}" for index, line in enumerate(spec["items"], start=1)]
    return {
        "action": key,
        "title": spec["title"],
        "lead": spec["lead"],
        "items": items,
        "requires_verify": spec["requires_verify"],
        "simulation": spec["simulation"],
        "button": spec["button"],
    }


def allows_apply(action: str, body: dict) -> bool:
    plan = plan_for(action, "region")
    if plan is None:
        return False
    if plan["requires_verify"]:
        return body.get("verify") is True
    return body.get("simulate") is True


def simulated_repair_items() -> list[str]:
    plan = plan_for("close", "the configured region")
    return list(plan["items"]) if plan else []


NOT_RUNNABLE = "The proposed action is not one this page can run."
NOT_RUN = "The action was not run."
_TABLE_RESTORE = re.compile(r"TABLE_NAME\s*(=|to)\s*enterprise-sites\b", re.IGNORECASE)
_TABLE_BREAK = re.compile(r"TABLE_NAME\s*(=|to)\s*missing-table\b", re.IGNORECASE)


def explicit_lab_target(resource_id: str, name: str, lab_targets: set[str] | frozenset[str]) -> bool:
    """A redrive target must be listed and must not be a live sre-agent queue."""
    target = (resource_id or "").strip()
    if not target or target not in lab_targets:
        return False
    blob = f"{target} {name}".lower()
    return "sre-agent" not in blob


def ticket_decision(
    proposed: str,
    *,
    summary: str = "",
    resource_name: str = "",
    resource_id: str = "",
    resource_type: str = "",
    lab_targets: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """Choose the one known action, or refuse. This does not call AWS or Jira."""
    text = proposed or ""
    blob = " ".join([text, summary or "", resource_name or "", resource_id or ""])
    about_directory = resource_name == "enterprise-sites-directory" or "enterprise-sites-directory" in blob
    restores_table = bool(_TABLE_RESTORE.search(text)) and not _TABLE_BREAK.search(text)
    if about_directory and restores_table and (resource_type in {"", "lambda:function"}):
        return {"kind": "restore-directory", "run": True, "target": resource_id, "message": ""}
    if "sqs.redrive_dlq" in text:
        target = resource_id if explicit_lab_target(resource_id, resource_name, lab_targets) else ""
        if not target:
            for item in sorted(lab_targets):
                if item and item in blob and explicit_lab_target(item, resource_name, lab_targets):
                    target = item
                    break
        if target:
            return {"kind": "redrive", "run": True, "target": target, "message": ""}
        return {"kind": "redrive", "run": False, "target": resource_id, "message": NOT_RUN}
    return {"kind": "none", "run": False, "target": "", "message": NOT_RUNNABLE}


def ticket_plan(key: str, decision: dict, region: str, proposed: str = "") -> dict:
    place = region.strip() or "the configured region"
    edited = (proposed or "").strip() or "No proposed action was entered."
    edited_line = f"Edited action: {edited}"
    if decision["run"] and decision["kind"] == "restore-directory":
        return _plan(
            "ticket",
            f"Take this action on {key}",
            "Nothing changes until you press Verify and apply.",
            [
                edited_line,
                f"Set Lambda function enterprise-sites-directory TABLE_NAME to enterprise-sites in {place}.",
                "Run inventory for that function.",
                "Close the Jira issue only if its health is healthy.",
            ],
            will_run=True,
        )
    if decision["run"] and decision["kind"] == "redrive":
        return _plan(
            "ticket",
            f"Take this action on {key}",
            "Nothing changes until you press Verify and apply.",
            [
                edited_line,
                f"Run sqs.redrive_dlq on the lab target in {place}.",
                "Run inventory for that queue.",
                "Close the Jira issue only if its health is healthy.",
            ],
            will_run=True,
        )
    refused = "The edited action was not run. The ticket stays open."
    return _plan("ticket", f"Take this action on {key}", refused, [edited_line, refused], will_run=False)


def lab_target_ids() -> frozenset[str]:
    from sre_agent.local_config import get

    return frozenset(item.strip() for item in get("LAB_TARGETS").split(",") if item.strip())


def _plan(action: str, title: str, lead: str, items: list[str], *, will_run: bool) -> dict:
    numbered = [f"{index}. {line}" for index, line in enumerate(items, start=1)]
    return {
        "action": action,
        "title": title,
        "lead": lead,
        "items": numbered,
        "requires_verify": True,
        "simulation": False,
        "button": "Verify and apply",
        "will_run": will_run,
        "ok": True,
        "applied": False,
    }


def parse_emails(text: str) -> tuple[list[str], list[str]]:
    """Split comma-separated or line-separated addresses. Returns valid, invalid."""
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,\n;]+", text or ""):
        item = part.strip()
        if not item:
            continue
        folded = item.lower()
        if EMAIL_RE.fullmatch(item):
            if folded not in seen:
                seen.add(folded)
                valid.append(item)
            continue
        if folded not in seen:
            seen.add(folded)
            invalid.append(item)
    return valid, invalid
