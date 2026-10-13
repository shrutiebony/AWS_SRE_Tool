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
_TABLE_RESTORE = re.compile(r"TABLE_NAME\s*(=|to)\s*(enterprise-sites|sre-dir-lab)\b", re.IGNORECASE)
_TABLE_BREAK = re.compile(r"TABLE_NAME\s*(=|to)\s*missing-table\b", re.IGNORECASE)
_DIRECTORY_TABLES = (
    ("enterprise-sites-directory", "enterprise-sites"),
    ("sre-dir-lab", "sre-dir-lab"),
)
_RESTORE_SENTENCE = "Set Lambda function enterprise-sites-directory TABLE_NAME to enterprise-sites."
_REDRIVE_SENTENCE = "Run sqs.redrive_dlq on the lab target. Move messages from the dead-letter queue back to the source queue."


def is_instruction(text: str) -> bool:
    """The placeholder sentence is not a correction."""
    return "name the configuration or metric change" in (text or "").lower()


def concrete_correction(view: dict) -> str:
    """The correction this page can run. Arbitrary prose and checker queues are not corrections."""
    from sre_agent.diagnosis.rules import is_checker_machinery

    resource_id = str(view.get("resource_id") or "")
    name = str(view.get("resource_name") or "")
    where_seen = str(view.get("where_seen") or view.get("task") or "")
    if is_checker_machinery(resource_id, name):
        return ""
    pair = directory_pair(name, resource_id)
    if pair and str(view.get("resource_type") or "") in {"", "lambda:function"}:
        return f"Set Lambda function {pair[0]} TABLE_NAME to {pair[1]}."
    target = _lab_target(resource_id, name, where_seen, lab_target_ids(), f"{resource_id} {where_seen}")
    if target:
        return _REDRIVE_SENTENCE
    return ""


def resolved_proposal(view: dict, proposed: str = "") -> str:
    """Show a runnable correction. The placeholder instruction is never shown."""
    text = (proposed or str(view.get("proposed") or "")).strip()
    if text and not is_instruction(text):
        return text
    return concrete_correction(view)


def directory_restore_function(proposed: str) -> str:
    """The function named by a TABLE_NAME restore. A break sentence is not a restore."""
    if _TABLE_BREAK.search(proposed or ""):
        return ""
    named = _TABLE_RESTORE.search(proposed or "")
    if named is None:
        return ""
    if named.group(2).lower() == "enterprise-sites":
        return "enterprise-sites-directory"
    return "sre-dir-lab"


def directory_pair(resource_name: str, resource_id: str) -> tuple[str, str] | None:
    """A directory function and the real table name the page can restore."""
    for function, table in _DIRECTORY_TABLES:
        if resource_name == function or f"function:{function}" in resource_id:
            return function, table
    return None


def _directory_resource(resource_name: str, resource_id: str, resource_type: str) -> bool:
    if resource_type not in {"", "lambda:function"}:
        return False
    return directory_pair(resource_name, resource_id) is not None


def explicit_lab_target(resource_id: str, name: str, lab_targets: set[str] | frozenset[str]) -> bool:
    """A redrive target must be listed and must not be a live sre-agent queue."""
    target = (resource_id or "").strip()
    if not target or target not in lab_targets:
        return False
    blob = f"{target} {name}".lower()
    return "sre-agent" not in blob


def _lab_target(resource_id: str, resource_name: str, where_seen: str, lab_targets, blob: str) -> str:
    if explicit_lab_target(resource_id, resource_name, lab_targets):
        return resource_id
    for item in sorted(lab_targets):
        if item and item in blob and explicit_lab_target(item, resource_name, lab_targets):
            return item
    if not resource_id and not resource_name:
        for item in sorted(lab_targets):
            if item and item in where_seen and explicit_lab_target(item, "", lab_targets):
                return item
    return ""


def _restore_decision(resource_id: str, function_name: str = "", table_name: str = "") -> dict:
    pair = directory_pair(function_name, resource_id) or ("enterprise-sites-directory", "enterprise-sites")
    return {
        "kind": "restore-directory",
        "run": True,
        "target": resource_id,
        "function_name": function_name or pair[0],
        "table_name": table_name or pair[1],
        "message": "",
    }


def _redrive_decision(target: str) -> dict:
    return {"kind": "redrive", "run": True, "target": target, "message": ""}


def _none_decision() -> dict:
    return {"kind": "none", "run": False, "target": "", "message": NOT_RUNNABLE}


def ticket_decision(
    proposed: str,
    *,
    summary: str = "",
    resource_name: str = "",
    resource_id: str = "",
    resource_type: str = "",
    lab_targets: set[str] | frozenset[str] = frozenset(),
    where_seen: str = "",
) -> dict:
    """Choose the runnable correction for this ticket. Arbitrary prose is not executed."""
    from sre_agent.diagnosis.rules import is_checker_machinery

    text = proposed or ""
    blob = " ".join([text, summary or "", resource_name or "", resource_id or "", where_seen or ""])
    if is_checker_machinery(resource_id, resource_name):
        return _none_decision()
    restores_table = bool(_TABLE_RESTORE.search(text)) and not _TABLE_BREAK.search(text)
    pair = directory_pair(resource_name, resource_id)
    directory = _directory_resource(resource_name, resource_id, resource_type)
    if restores_table and resource_type in {"", "lambda:function"} and (directory or not resource_name):
        named = _TABLE_RESTORE.search(text)
        table_named = named.group(2) if named else ""
        if table_named.lower() == "sre-dir-lab":
            return _restore_decision(resource_id, "sre-dir-lab", "sre-dir-lab")
        function_name, table_name = pair or ("enterprise-sites-directory", "enterprise-sites")
        return _restore_decision(resource_id, function_name, table_name)
    if "sqs.redrive_dlq" in text:
        target = _lab_target(resource_id, resource_name, where_seen, lab_targets, blob)
        if target:
            return _redrive_decision(target)
    if directory and pair:
        return _restore_decision(resource_id, pair[0], pair[1])
    target = _lab_target(resource_id, resource_name, where_seen, lab_targets, blob)
    if target:
        return _redrive_decision(target)
    return _none_decision()


def ticket_plan(key: str, decision: dict, region: str, proposed: str = "") -> dict:
    place = region.strip() or "the configured region"
    if decision["run"] and decision["kind"] == "restore-directory":
        function_name = decision.get("function_name") or "enterprise-sites-directory"
        table_name = decision.get("table_name") or "enterprise-sites"
        return _plan(
            "ticket",
            f"Take this action on {key}",
            "Nothing changes until you press Verify and apply.",
            [
                f"Set Lambda function {function_name} TABLE_NAME to {table_name} in {place}.",
                "Read TABLE_NAME on that function. Close the ticket when it is the real table.",
                "Update the Jira issue. Close it only after that read shows the real table.",
            ],
            will_run=True,
        )
    if decision["run"] and decision["kind"] == "redrive":
        return _plan(
            "ticket",
            f"Take this action on {key}",
            "Nothing changes until you press Verify and apply.",
            [
                f"Run sqs.redrive_dlq on the lab target in {place}.",
                "Read that queue's visible count and dead-letter depth a few times.",
                "Update the Jira issue. Close it only when both counts are zero.",
            ],
            will_run=True,
        )
    return _plan(
        "ticket",
        f"Take this action on {key}",
        "This issue has no correction this page can run.",
        ["A correction runs only for a lab queue redrive or a directory TABLE_NAME restore."],
        will_run=False,
    )


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
