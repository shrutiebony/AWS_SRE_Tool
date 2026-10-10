"""Saved demo recipients and the SES send. Addresses stay in a gitignored file."""

from __future__ import annotations

import json
import os
from pathlib import Path

from sre_agent.demo_plans import parse_emails
from sre_agent.local_config import ROOT

RECIPIENTS_PATH = ROOT / "demo_ui" / "recipients.local.json"
EMPTY_MAIL = "Mail was not sent because no address is saved."
SIMULATION_MAIL = "This simulation does not send mail and does not change the live account."
CLEANUP_MAIL = "Cleanup does not send mail."


def load_recipients() -> list[str]:
    if not RECIPIENTS_PATH.is_file():
        return []
    try:
        payload = json.loads(RECIPIENTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    raw = payload.get("addresses") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    valid, _invalid = parse_emails("\n".join(str(item) for item in raw))
    return valid


def save_recipients(text: str) -> dict:
    valid, invalid = parse_emails(text)
    if invalid:
        listed = ", ".join(invalid)
        return {
            "ok": False,
            "addresses": load_recipients(),
            "message": f"These addresses are not valid email addresses: {listed}",
        }
    RECIPIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECIPIENTS_PATH.write_text(json.dumps({"addresses": valid}, indent=2) + "\n", encoding="utf-8")
    if not valid:
        message = "No addresses are saved. Mail will not be sent."
    else:
        message = f"Saved {len(valid)} address(es)."
    return {"ok": True, "addresses": valid, "message": message}


def ticket_mail(diagnosis, key: str) -> str:
    """Mail the new ticket. An empty address list does not call AWS."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return EMPTY_MAIL
    from sre_agent.integrations.jira import star_plain

    from sre_agent.local_config import get

    base = get("JIRA_BASE_URL").rstrip("/")
    link = f"{base}/browse/{key}" if base and key else key
    body = (
        f"A Jira ticket was created.\n\nTicket: {key}\nLink: {link}\n\n"
        f"{star_plain(diagnosis)}\n\n"
        "Verify the proposed action before any change. The ticket closes after that action is performed."
    )
    return deliver_mail(f"SRE agent: {key} created", body)


def deliver_mail(subject: str, body: str) -> str:
    """Send through SES. An empty recipient list does not call AWS."""
    addresses = load_recipients()
    if not addresses:
        return EMPTY_MAIL
    from sre_agent.local_config import get

    sender = get("SES_FROM_ADDRESS")
    if not sender:
        return "Mail was not sent because the SES from-address is not in the local config."
    region = get("SRE_REGION") or get("AWS_REGION")
    if not region:
        return "Mail was not sent because the region is not in the local config."
    import boto3

    client = boto3.client("sesv2", region_name=region)
    try:
        client.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": addresses},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                }
            },
        )
    except Exception as exc:
        return "Mail was not sent. " + _safe(str(exc))
    return "Mail was sent to the saved addresses."


def _safe(text: str) -> str:
    cleaned = text
    for key, value in os.environ.items():
        upper = key.upper()
        if not value or len(value) < 8:
            continue
        if any(part in upper for part in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "GEMINI", "AWS_ACCESS", "AWS_SESSION")):
            cleaned = cleaned.replace(value, "[redacted]")
    from sre_agent.local_config import get

    sender = get("SES_FROM_ADDRESS")
    if sender:
        cleaned = cleaned.replace(sender, "[from-address]")
    return cleaned[:500]
