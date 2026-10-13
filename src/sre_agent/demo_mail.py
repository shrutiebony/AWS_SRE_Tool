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


def add_recipient(address: str) -> dict:
    """Save one address. An invalid address does not change the file."""
    valid, invalid = parse_emails(address)
    current = load_recipients()
    if invalid or not valid:
        listed = ", ".join(invalid) if invalid else "that value"
        return {
            "ok": False,
            "addresses": current,
            "message": f"These addresses are not valid email addresses: {listed}",
        }
    chosen = valid[0]
    if chosen.lower() in {item.lower() for item in current}:
        return {"ok": True, "addresses": current, "message": "That address is already saved."}
    saved = save_recipients("\n".join([*current, chosen]))
    saved["message"] = f"Saved {chosen}."
    return saved


def delete_recipient(address: str) -> dict:
    """Remove one saved address and write the file. Other addresses stay."""
    current = load_recipients()
    chosen = (address or "").strip()
    if not chosen:
        return {"ok": False, "addresses": current, "message": "No addresses are saved."}
    kept = [item for item in current if item.lower() != chosen.lower()]
    if len(kept) == len(current):
        return {"ok": False, "addresses": current, "message": "That address is not in the saved list."}
    saved = save_recipients("\n".join(kept))
    saved["message"] = "Removed that address."
    return saved


def confirm_verification(address: str) -> dict:
    """Check Amazon, and send the verification message when the address is not verified yet."""
    current = verification_status(address)
    if not current.get("ok") or current.get("status") == "Success":
        return current
    return request_verification(address)


def request_verification(address: str) -> dict:
    """Ask SES to email a verification link. Pytest does not call SES."""
    chosen = (address or "").strip()
    valid, invalid = parse_emails(chosen)
    if invalid or not valid:
        return {"ok": False, "status": "", "address": chosen, "message": "That value is not an email address."}
    chosen = valid[0]
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("SRE_TEST_MAIL") != "1":
        return {
            "ok": True,
            "status": "Pending",
            "address": chosen,
            "message": f"Amazon will send a verification message to {chosen}. Open it and click the link, then press Check verification.",
        }
    try:
        _ses_v1().verify_email_identity(EmailAddress=chosen)
    except Exception as exc:
        return {"ok": False, "status": "", "address": chosen, "message": _identity_failure(exc)}
    return {
        "ok": True,
        "status": "Pending",
        "address": chosen,
        "message": f"Amazon sent a verification message to {chosen}. Open it and click the link, then press Check verification.",
    }


def verification_status(address: str) -> dict:
    """Read whether SES has verified this address. Pytest does not call SES."""
    chosen = (address or "").strip()
    valid, invalid = parse_emails(chosen)
    if invalid or not valid:
        return {"ok": False, "status": "", "address": chosen, "message": "That value is not an email address."}
    chosen = valid[0]
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("SRE_TEST_MAIL") != "1":
        return {
            "ok": True,
            "status": "Pending",
            "address": chosen,
            "message": f"Amazon is still waiting for {chosen} to click the verification link.",
        }
    try:
        attributes = _ses_v1().get_identity_verification_attributes(Identities=[chosen])
    except Exception as exc:
        return {"ok": False, "status": "", "address": chosen, "message": _identity_failure(exc)}
    found = {}
    if isinstance(attributes, dict):
        found = (attributes.get("VerificationAttributes") or {}).get(chosen) or {}
    status = str(found.get("VerificationStatus") or "NotStarted")
    return {"ok": True, "status": status, "address": chosen, "message": _verification_message(chosen, status)}


def _verification_message(address: str, status: str) -> str:
    if status == "Success":
        return f"{address} is verified. Mail can be sent to it."
    if status == "Pending":
        return f"Amazon is still waiting for {address} to click the verification link."
    if status == "Failed":
        return f"Verification for {address} failed. Send the verification email again."
    return f"Amazon has not verified {address} yet. Send the verification email, then click the link in that message."


def _ses_client(service: str):
    """A new session, so a refreshed login is not hidden by an older boto3 client."""
    import boto3

    from sre_agent.local_config import get

    region = get("SRE_REGION") or get("AWS_REGION")
    if not region:
        raise RuntimeError("region missing")
    return boto3.Session(region_name=region).client(service, region_name=region)


def _ses_v1():
    return _ses_client("ses")


def applied_mail(key: str, note: str) -> str:
    """Mail a verified action through SES.

    A normal pytest run does not call SES. A mail test sets SRE_TEST_MAIL=1 and
    supplies its own client. An empty address list still does not call SES.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("SRE_TEST_MAIL") != "1":
        return ""
    return deliver_mail(f"SRE agent: {key} action applied", note or key)


def repair_mail(key: str, happened: str, cause: str, action: str, delta: str) -> str:
    """Mail the verified repair: what happened, the root cause, the change, and the delta."""
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("SRE_TEST_MAIL") != "1":
        return ""
    body = (
        f"What happened\n{happened}\n\n"
        f"Root cause\n{cause}\n\n"
        f"What we did\n{action}\n\n"
        f"Delta\n{delta}\n"
    )
    return deliver_mail(f"SRE agent: {key} action applied", body)


CLOSE_ADDRESS = "ordersre840@gmail.com"


def close_mail(key: str, happened: str = "", cause: str = "", action: str = "", delta: str = "") -> str:
    """Send the close mail to the fixed address as soon as the ticket closes."""
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("SRE_TEST_MAIL") != "1":
        return ""
    if happened:
        body = (
            f"What happened\n{happened}\n\n"
            f"Root cause\n{cause}\n\n"
            f"What we did\n{action}\n\n"
            f"Delta\n{delta}\n"
        )
    else:
        body = f"Jira {key} was closed."
    note = deliver_mail(
        f"SRE agent: {key} closed",
        body,
        addresses=[CLOSE_ADDRESS],
        sender=CLOSE_ADDRESS,
    )
    if note == "Mail was sent to the saved addresses.":
        return f"Mail was sent to {CLOSE_ADDRESS}."
    return note


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


def deliver_mail(
    subject: str,
    body: str,
    addresses: list[str] | None = None,
    sender: str | None = None,
) -> str:
    """Send through SES. An empty recipient list does not call AWS.

    The configured from-address stays required so an unfinished setup still
    stops here. It is not used as the From header: that domain rejects mail
    that Amazon sends, and the recipient never sees it. The verified inbox
    address is the From header.
    """
    chosen = [item.strip() for item in (addresses if addresses is not None else load_recipients()) if str(item).strip()]
    if not chosen:
        return EMPTY_MAIL
    from sre_agent.local_config import get

    if not get("SES_FROM_ADDRESS"):
        return "Mail was not sent because the SES from-address is not in the local config."
    from_address = (sender or CLOSE_ADDRESS).strip()
    region = get("SRE_REGION") or get("AWS_REGION")
    if not region:
        return "Mail was not sent because the region is not in the local config."
    client = _ses_client("sesv2")
    try:
        response = client.send_email(
            FromEmailAddress=from_address,
            Destination={"ToAddresses": chosen},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                }
            },
        )
    except Exception as exc:
        return _ses_failure(exc, from_address)
    message_id = ""
    if isinstance(response, dict):
        message_id = str(response.get("MessageId") or "").strip()
    if not message_id:
        return "Mail was not sent because SES did not accept the message."
    return "Mail was sent to the saved addresses."


def _identity_failure(exc: BaseException) -> str:
    """One sentence for the verify popup. This is not a ticket send."""
    text = str(exc).lower()
    if "expiredtoken" in text or "security token included in the request is expired" in text:
        return "Amazon could not check this address because the AWS login expired. Run aws login, then press Verify address again."
    message = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if isinstance(error, dict):
            message = str(error.get("Message") or "")
    detail = _safe(message or str(exc)).split("\n", 1)[0].strip().rstrip(".")
    if not detail:
        return "Amazon could not check this address."
    return f"Amazon could not check this address: {detail[:180]}."


def _ses_failure(exc: BaseException, sender: str) -> str:
    """One sentence for the page. The from-address is never included."""
    code = ""
    message = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if isinstance(error, dict):
            code = str(error.get("Code") or "")
            message = str(error.get("Message") or "")
    lowered = f"{code} {message}".lower()
    sender_lower = (sender or "").strip().lower()
    mentions_sender = bool(sender_lower) and sender_lower in lowered
    other_address = "@" in lowered.replace(sender_lower, "")
    if "not verified" in lowered or code in {"MessageRejected", "MailFromDomainNotVerifiedException"}:
        if mentions_sender and not other_address:
            return "Mail was not sent because the SES from-address is not verified."
        if "sandbox" in lowered or "not verified" in lowered:
            return "Mail was not sent because SES rejected an unverified recipient while the account is in the sandbox."
        return "Mail was not sent because SES rejected an unverified recipient."
    if "sending paused" in lowered or code == "AccountSendingPausedException":
        return "Mail was not sent because SES sending is paused for this account."
    detail = _safe(message or str(exc)).split("\n", 1)[0].strip().rstrip(".")
    if sender:
        detail = detail.replace(sender, "[from-address]")
    if not detail:
        return "Mail was not sent because SES returned an error."
    return f"Mail was not sent because SES returned an error: {detail[:180]}."


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
