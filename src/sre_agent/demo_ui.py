"""Local click-through page for the live inventory and the lab.

The page calls the same operations cycle as ``python -m sre_agent inventory``.
It does not score health on its own. Bind it to 127.0.0.1:8765.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sre_agent.cli import run_live_cycle
from sre_agent.demo_mail import (
    CLEANUP_MAIL,
    SIMULATION_MAIL,
    add_recipient,
    applied_mail,
    delete_recipient,
    deliver_mail,
    load_recipients,
    confirm_verification,
    close_mail,
    save_recipients,
    verification_status,
)
from sre_agent.demo_plans import (
    NOT_RUN,
    NOT_RUNNABLE,
    ALIASES,
    allows_apply,
    directory_restore_function,
    explicit_lab_target,
    is_instruction,
    lab_target_ids,
    plan_for,
    resolved_proposal,
    ticket_decision,
    ticket_plan,
)
from sre_agent.integrations.confluence import FakeConfluence, catalog_sections, load_catalog
from sre_agent.local_config import get, load_local_config

HOST = "127.0.0.1"
PORT = 8765
REGION = ""
ACCOUNT = ""
DIRECTORY = "enterprise-sites-directory"
DIRECTORY_ABSENT = "The directory function is not in this account."
LIVE_TABLE = "enterprise-sites"
EXTRA_TABLE = "demo-extra-sites"
TOPIC_NAME = "demo-alerts"
SITES_URL = ""
ROOT = Path(__file__).resolve().parents[2]
SNS_HEADING = "Service: SNS (sns:topic)"
EKS_HEADING = "Service: EKS (eks:cluster)"
SNS_COLUMNS = (
    "Symptom ID",
    "Metric / query",
    "Condition",
    "Threshold",
    "Status",
    "Type",
    "Namespace / log group",
    "Dimension",
    "Stat",
    "Window (min)",
    "Role",
)
SNS_ROW = (
    "SNS-S01",
    "NumberOfNotificationsFailed",
    ">",
    "0",
    "active",
    "metric",
    "AWS/SNS",
    "TopicName={name}",
    "Sum",
    "5",
    "primary",
)

_SECRET_NAMES = (
    "JIRA_TOKEN",
    "CONFLUENCE_TOKEN",
    "GEMINI_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "JIRA_EMAIL",
    "CONFLUENCE_EMAIL",
)


def public_text(value: object) -> str:
    """Remove credential material before anything is shown or logged."""
    text = str(value)
    for name in _SECRET_NAMES:
        secret = os.environ.get(name, "")
        if len(secret) >= 8 and secret in text:
            text = text.replace(secret, "[redacted]")
    text = re.sub(
        r"(?i)(api[_-]?key|token|password|secret|sessiontoken|authorization)=([^&\s\"']+)",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b", "[redacted]", text)
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    return text[:2000]


def sns_storage() -> str:
    header = "".join(f"<th>{html.escape(name)}</th>" for name in SNS_COLUMNS)
    cells = "".join(f"<td>{html.escape(value)}</td>" for value in SNS_ROW)
    return f"<h2>{html.escape(SNS_HEADING)}</h2><table><tr>{header}</tr><tr>{cells}</tr></table>"


def bind_settings() -> None:
    """Read account, region, and the sites URL from local config. Already-set variables win."""
    global ACCOUNT, REGION, SITES_URL
    load_local_config()
    ACCOUNT = get("SRE_ACCOUNT_ID")
    REGION = get("SRE_REGION")
    SITES_URL = get("SRE_SITES_URL")
    profile = get("AWS_PROFILE")
    if profile:
        os.environ.setdefault("AWS_PROFILE", profile)
        os.environ.setdefault("AWS_DEFAULT_PROFILE", profile)


def sns_plain() -> str:
    lines = [SNS_HEADING, "\t".join(SNS_COLUMNS), "\t".join(SNS_ROW)]
    return "\n".join(lines)


TYPE_LABELS = {
    "lambda:function": "Lambda",
    "dynamodb:table": "DynamoDB",
    "sqs:queue": "SQS",
    "apigateway:restapi": "API Gateway",
    "sns:topic": "SNS",
    "ec2:instance": "EC2",
}


def type_label(resource_type: str) -> str:
    return TYPE_LABELS.get(resource_type, resource_type)


def inventory_groups(rows: list[dict]) -> list[dict]:
    """Group resources the scan found. Catalog headings are not a group source."""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("presence") not in {"present", ""}:
            continue
        buckets.setdefault(row.get("resource_type") or "unknown", []).append(row)
    ordered = [item for item in TYPE_LABELS if item in buckets]
    ordered.extend(sorted(item for item in buckets if item not in TYPE_LABELS))
    return [
        {"resource_type": item, "label": type_label(item), "rows": buckets[item]}
        for item in ordered
    ]


class Demo:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self.phase = "Starting."
        self.message = ""
        self.error = ""
        self.report = None
        self.rows: list[dict] = []
        self.scanned_at = ""
        self.catalog_status = "not-configured"
        self.catalog_types: list[str] = []
        self.catalog_sections: list[dict] = []
        self.catalog_read_error = ""
        self.scan_notes: list[str] = []
        self.directory_issue_key = ""
        self.directory_note = ""
        self.change_note = ""
        self.topic_note = ""
        self.lab_output = ""
        self.lab_code: int | None = None
        self.mail_note = ""
        self.catalog_note = ""
        self.ticket_notes: dict[str, str] = {}
        self.issue_links: dict[str, dict] = {}
        self.issue_views: list[dict] = []
        self.jira_error = ""
        self.jira_client = None
        self.directory_closed = False
        self.directory_changed = False
        self.redrive_settle_attempts = 4
        self.redrive_settle_pause = 3

    def set_phase(self, text: str, *, log: bool = False) -> None:
        safe = public_text(text)
        with self._lock:
            self.phase = safe
        if log:
            print(safe, flush=True)

    def status(self) -> dict:
        with self._lock:
            busy = self._busy
            message = self.phase
            error = self.error
        return {"busy": busy, "message": message, "error": error}

    def try_begin(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self.error = ""
            self.mail_note = ""
            return True

    def finish(self, message: str) -> None:
        text = message
        if self.mail_note:
            text = message + " " + self.mail_note
        safe = public_text(text)
        with self._lock:
            self.message = safe
            self.phase = safe
            self.error = ""
            self._busy = False
        print(safe, flush=True)

    def fail(self, exc: BaseException) -> None:
        detail = DIRECTORY_ABSENT if _directory_waiter_error(exc) else _error_text(exc)
        with self._lock:
            self.error = detail
            self.message = "The last action failed."
            self.phase = "The last action failed. " + detail
            self._busy = False
        print("The last action failed. " + detail, flush=True)

    def run_baseline(self) -> None:
        self.set_phase("Running baseline inventory.", log=True)
        self._inventory()
        count = len(self.rows)
        self.finish(f"Baseline inventory completed. {count} resources.")
        self._log_rows()

    def open_health(self) -> None:
        """Read the account before drawing the page. A return from Jira reads it again."""
        self.ensure_fresh()

    def ensure_fresh(self) -> None:
        """Read the account before drawing the page. A previous scan is not reused."""
        while not self.try_begin():
            time.sleep(0.25)
        try:
            self._display_scan()
        except Exception as exc:
            if not _expired_token(exc):
                self.fail(exc)
                return
            try:
                refresh_aws_login(force=True)
                self._display_scan()
            except Exception as retry_exc:
                self.fail(retry_exc)

    def _display_scan(self) -> None:
        self.set_phase("Running inventory.", log=True)
        self._inventory(display=True)
        self._read_catalog()
        count = sum(1 for row in self.rows if row.get("presence") in {"present", ""})
        self.finish(f"Baseline inventory completed. {count} resources.")
        self._log_rows()

    def _log_rows(self) -> None:
        for row in self.rows:
            code = row["problem_code"] or "-"
            print(f"  {row['resource_type']}  {row['name']}  {row['health']}  {code}", flush=True)
        if self.directory_issue_key:
            print("Directory issue " + self.directory_issue_key, flush=True)

    def add_resource(self) -> None:
        self.set_phase("Creating DynamoDB table demo-extra-sites.", log=True)
        refresh_aws_login()
        import boto3

        client = boto3.Session(region_name=REGION).client("dynamodb", region_name=REGION)
        try:
            client.create_table(
                TableName=EXTRA_TABLE,
                AttributeDefinitions=[{"AttributeName": "site_id", "AttributeType": "S"}],
                KeySchema=[{"AttributeName": "site_id", "KeyType": "HASH"}],
                BillingMode="PAY_PER_REQUEST",
            )
        except client.exceptions.ResourceInUseException as exc:
            raise RuntimeError("demo-extra-sites already exists.") from exc
        self.set_phase("Waiting until demo-extra-sites is active.", log=True)
        client.get_waiter("table_exists").wait(
            TableName=EXTRA_TABLE,
            WaiterConfig={"Delay": 5, "MaxAttempts": 24},
        )
        self.set_phase("Running inventory.", log=True)
        self._inventory()
        present = any(row["name"] == EXTRA_TABLE and row["resource_type"] == "dynamodb:table" for row in self.rows)
        if present:
            self.change_note = "Created demo-extra-sites and ran inventory. The table is in the registry."
        else:
            self.change_note = "Created demo-extra-sites and ran inventory. The table is not in this inventory result."
        self.mail_note = deliver_mail("SRE agent: demo-extra-sites", self.change_note)
        self.finish(self.change_note)

    def remove_resource(self) -> None:
        self.set_phase("Deleting DynamoDB table demo-extra-sites.", log=True)
        refresh_aws_login()
        import boto3

        client = boto3.Session(region_name=REGION).client("dynamodb", region_name=REGION)
        try:
            client.delete_table(TableName=EXTRA_TABLE)
        except client.exceptions.ResourceNotFoundException as exc:
            raise RuntimeError("demo-extra-sites is already gone.") from exc
        self.set_phase("Waiting until demo-extra-sites is gone.", log=True)
        client.get_waiter("table_not_exists").wait(
            TableName=EXTRA_TABLE,
            WaiterConfig={"Delay": 5, "MaxAttempts": 24},
        )
        self.set_phase("Running inventory.", log=True)
        self._inventory()
        present = any(row["name"] == EXTRA_TABLE and row["resource_type"] == "dynamodb:table" for row in self.rows)
        if present:
            self.change_note = "Deleted demo-extra-sites and ran inventory. It is still listed."
        else:
            self.change_note = "Deleted demo-extra-sites and ran inventory. It is gone from the registry."
        self.mail_note = deliver_mail("SRE agent: demo-extra-sites removed", self.change_note)
        self.finish(self.change_note)

    def break_directory(self) -> None:
        self.set_phase("Pointing enterprise-sites-directory at TABLE_NAME=missing-table.", log=True)
        self._set_directory_table("missing-table")
        self.set_phase("Configuration update is active. Invoking the sites URL.", log=True)
        codes = _invoke_sites()
        self.set_phase("Sites URL responses: " + _codes(codes) + ". Waiting for the Lambda Errors metric.", log=True)
        mark = datetime.now(timezone.utc)

        def ready() -> bool:
            points = _lambda_metric("Errors", mark - timedelta(minutes=3))
            return any(
                _aware(point["Timestamp"]) >= mark - timedelta(seconds=90) and float(point.get("Sum") or 0) > 0
                for point in points
            )

        seen = self._wait(minimum=60, maximum=90, label="Waiting for the Lambda Errors metric.", ready=ready)
        self.set_phase("Running inventory.", log=True)
        self._inventory()
        health, code = self._directory_health()
        key = self.directory_issue_key
        link = _jira_url(key)
        if seen:
            metric = "CloudWatch published Lambda errors."
        else:
            metric = "Waited 90 seconds and the Errors metric was not visible yet."
        if key:
            opened = f"Jira issue {key}."
        else:
            opened = "Inventory did not return a Jira issue key."
        self.directory_note = f"Responses { _codes(codes) }. {metric} Directory health is {health or 'not in this inventory'}. Problem code {code or 'none'}. {opened}"
        if link:
            self.directory_note += " Link is on this panel."
        self.mail_note = deliver_mail("SRE agent: enterprise-sites-directory unhealthy", self.directory_note)
        self.finish(self.directory_note)

    def restore_directory(
        self,
        *,
        mail: bool = True,
        close: str = "always",
        issue_key: str = "",
        display: bool = False,
        function_name: str = "",
        table_name: str = "",
    ) -> None:
        del display
        function_name = function_name or DIRECTORY
        table_name = table_name or LIVE_TABLE
        note, closed = self._apply_directory_restore(function_name, table_name, issue_key)
        self.directory_note = note
        self.directory_closed = closed
        if close == "healthy":
            return
        if mail:
            self.mail_note = deliver_mail(f"SRE agent: {function_name} restored", note)
        self.finish(note)

    def show_close(self) -> None:
        self.set_phase("Running python -m sre_agent lab.", log=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env.pop("SRE_SERVICE_LIST_URI", None)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "sre_agent", "lab"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("python -m sre_agent lab did not finish within 180 seconds.") from exc
        text = public_text(completed.stdout or "")
        if completed.stderr and completed.stderr.strip():
            text = (text + "\n" + public_text(completed.stderr)).strip()
        self.lab_output = text or "The lab produced no output."
        self.lab_code = completed.returncode
        self.mail_note = SIMULATION_MAIL
        if completed.returncode == 0:
            self.finish("Lab finished with exit code 0. This was a simulation. The live account was not changed.")
        else:
            self.finish(f"Lab finished with exit code {completed.returncode}. This was a simulation. The live account was not changed.")

    def create_topic(self) -> None:
        self.set_phase("Creating SNS topic demo-alerts.", log=True)
        refresh_aws_login()
        import boto3

        session = boto3.Session(region_name=REGION)
        sns = session.client("sns", region_name=REGION)
        arn = sns.create_topic(Name=TOPIC_NAME)["TopicArn"]
        sns.tag_resource(ResourceArn=arn, Tags=[{"Key": "Name", "Value": TOPIC_NAME}])
        self.set_phase("Running inventory.", log=True)
        self._inventory()
        self.topic_note = self._topic_finding()
        if "sns:topic" in self.catalog_types and not _topic_row(self.rows):
            self.set_phase("The heading is on the page. Waiting for the tagging API to list demo-alerts.", log=True)
            if _wait_for_topic(session):
                self.set_phase("Running inventory again.", log=True)
                self._inventory()
            self.topic_note = self._topic_finding()
        self.mail_note = deliver_mail("SRE agent: demo-alerts", self.topic_note)
        self.finish(self.topic_note)

    def delete_topic(self) -> None:
        self.set_phase("Deleting SNS topic demo-alerts.", log=True)
        note = self._delete_demo_topic()
        self.set_phase("Running inventory.", log=True)
        self._inventory()
        self.topic_note = note + " Inventory ran afterward."
        self.mail_note = deliver_mail("SRE agent: demo-alerts removed", self.topic_note)
        self.finish(self.topic_note)

    def reset_account(self) -> None:
        self.set_phase("Setting enterprise-sites-directory TABLE_NAME to enterprise-sites.", log=True)
        self._set_directory_table(LIVE_TABLE)
        self.set_phase("Deleting demo-extra-sites if it exists.", log=True)
        table_note = self._delete_extra_table()
        self.set_phase("Deleting demo-alerts if it exists.", log=True)
        topic_note = self._delete_demo_topic()
        self.set_phase("Running inventory.", log=True)
        self._inventory()
        self.change_note = (
            f"Restored TABLE_NAME to enterprise-sites. {table_note} {topic_note} Inventory ran afterward."
        )
        self.mail_note = CLEANUP_MAIL
        self.finish(self.change_note)

    def _delete_extra_table(self) -> str:
        _guard_demo_resource(EXTRA_TABLE)
        refresh_aws_login()
        import boto3

        client = boto3.Session(region_name=REGION).client("dynamodb", region_name=REGION)
        try:
            client.delete_table(TableName=EXTRA_TABLE)
        except client.exceptions.ResourceNotFoundException:
            return "demo-extra-sites was already gone."
        self.set_phase("Waiting until demo-extra-sites is gone.", log=True)
        client.get_waiter("table_not_exists").wait(
            TableName=EXTRA_TABLE,
            WaiterConfig={"Delay": 5, "MaxAttempts": 24},
        )
        return "Deleted demo-extra-sites."

    def _delete_demo_topic(self) -> str:
        _guard_demo_resource(TOPIC_NAME)
        refresh_aws_login()
        import boto3

        sns = boto3.Session(region_name=REGION).client("sns", region_name=REGION)
        arn = ""
        for page in sns.get_paginator("list_topics").paginate():
            for item in page.get("Topics") or []:
                candidate = str(item.get("TopicArn") or "")
                if candidate.rsplit(":", 1)[-1] == TOPIC_NAME:
                    if arn:
                        raise RuntimeError("More than one demo-alerts topic was listed.")
                    arn = candidate
        if not arn:
            return "demo-alerts was already gone."
        sns.delete_topic(TopicArn=arn)
        return "Deleted demo-alerts."

    def _inventory(self, *, display: bool = False) -> None:
        refresh_aws_login()
        os.environ.pop("SRE_SERVICE_LIST_URI", None)
        report, agent = run_live_cycle(ACCOUNT, REGION, display=display)
        if display:
            agent.publish_health_board(report)
        self.agent = agent
        note = getattr(agent, "last_mail_note", "") or ""
        if note:
            self.mail_note = note
        self._apply(report, agent)

    def _close_verified_ticket(self) -> str:
        key = self.directory_issue_key
        if not key:
            return "No directory ticket was closed."
        return self._close_issue(key, "after the verified action")

    def _close_issue(self, key: str, phrase: str = "because health is healthy") -> str:
        client = getattr(getattr(self, "agent", None), "jira", None)
        if client is None or not hasattr(client, "close"):
            from sre_agent.worker import _jira

            client = _jira()
        if not key or client is None or not hasattr(client, "close"):
            return "No ticket was closed."
        try:
            client.close(key)
        except Exception as exc:
            return f"{key} was not closed. {public_text(str(exc))}"
        self.mail_note = self._mail_on_close(key)
        self.close_mailed = True
        return f"{key} was closed {phrase}."

    def _mail_on_close(self, key: str) -> str:
        """The close mail goes to the fixed address, even when the saved list is empty."""
        report = getattr(self, "mail_report", None)
        if isinstance(report, dict):
            return close_mail(
                key,
                str(report.get("happened") or ""),
                str(report.get("cause") or ""),
                str(report.get("action") or ""),
                str(report.get("delta") or ""),
            )
        return close_mail(key)

    def _close_if_healthy(self, key: str, resource_id: str = "", name: str = "", resource_type: str = "") -> str:
        health, code = self._health_for(resource_id, name, resource_type)
        if health == "healthy":
            return self._close_issue(key)
        note = _stay_open(health, code)
        self._note_issue(key, "Verified action ran. " + note)
        return note

    def _note_issue(self, key: str, body: str) -> None:
        """Update the Jira issue. This does not close it."""
        if not key:
            return
        client = getattr(self, "jira_client", None)
        if client is None:
            client = getattr(getattr(self, "agent", None), "jira", None)
        if client is None or not hasattr(client, "add_note"):
            if os.environ.get("PYTEST_CURRENT_TEST"):
                return
            from sre_agent.worker import _jira

            client = _jira()
        if client is None or not hasattr(client, "add_note"):
            return
        client.add_note(key, body)

    def _health_for(self, resource_id: str = "", name: str = "", resource_type: str = "") -> tuple[str, str]:
        for row in self.rows:
            if resource_id and row.get("resource_id") == resource_id:
                return row.get("health") or "", row.get("problem_code") or ""
        for row in self.rows:
            if name and row.get("name") == name and (not resource_type or row.get("resource_type") == resource_type):
                return row.get("health") or "", row.get("problem_code") or ""
        return "", ""

    def take_issue(self, key: str, proposed: str = "") -> None:
        view = self._issue_view(key)
        if view is None:
            raise RuntimeError(f"{key} is not an open issue in this project.")
        view = {**view, "proposed": _posted_proposal(view, proposed)}
        decision = _decision_for(view)
        if not decision["run"]:
            note = "This issue has no correction this page can run."
            self.ticket_notes[key] = note
            self.finish(note)
            return
        if decision["kind"] == "restore-directory":
            self.restore_directory(
                mail=False,
                close="healthy",
                issue_key=key,
                display=True,
                function_name=str(decision.get("function_name") or ""),
                table_name=str(decision.get("table_name") or ""),
            )
            self._finish_verified(
                key,
                self.directory_note,
                self.directory_closed,
                applied=self.directory_changed,
            )
            return
        if decision["kind"] == "redrive":
            note = self._run_lab_redrive(decision["target"], view.get("resource_name", ""))
            closed, did_close = self._finish_redrive(
                key,
                decision["target"],
                view.get("resource_name", ""),
                note,
            )
            self._finish_verified(
                key,
                f"{note} {closed}".strip(),
                did_close,
                applied="sqs.redrive_dlq ran" in (note or ""),
            )
            return
        self.ticket_notes[key] = NOT_RUNNABLE
        self.finish(NOT_RUNNABLE)

    def _finish_verified(self, key: str, text: str, _closed: bool, *, applied: bool = True) -> None:
        """Mail once the verified AWS action has been applied, including when the ticket then closes."""
        self.ticket_notes[key] = text
        if getattr(self, "close_mailed", False):
            self.finish(text)
            return
        report = getattr(self, "mail_report", None)
        if "was closed" in text and isinstance(report, dict):
            self.mail_note = close_mail(
                key,
                str(report.get("happened") or ""),
                str(report.get("cause") or ""),
                str(report.get("action") or ""),
                str(report.get("delta") or ""),
            )
        elif "was closed" in text:
            self.mail_note = close_mail(key)
        elif applied:
            self.mail_note = applied_mail(key, text)
        else:
            self.mail_note = "Mail was not sent because the verified action was not applied."
        self.finish(text)

    def _finish_redrive(self, key: str, resource_id: str, name: str, note: str) -> tuple[str, bool]:
        """Read this queue a few times. Close only when visible messages and the DLQ depth are zero."""
        if "sqs.redrive_dlq ran" not in (note or ""):
            stayed = "The ticket stays open because sqs.redrive_dlq did not clear the queue."
            self._note_issue(key, "Verified action ran. " + note + " " + stayed)
            return stayed, False
        if "sre-agent" in f"{resource_id} {name}".lower():
            stayed = "The ticket stays open because an sre-agent queue is not redriven."
            self._note_issue(key, stayed)
            return stayed, False
        attempts = max(1, int(self.redrive_settle_attempts))
        pause = max(0.0, float(self.redrive_settle_pause))
        last = "The ticket stays open because the queue was not read."
        for attempt in range(1, attempts + 1):
            self.set_phase("Reading the lab queue after sqs.redrive_dlq.", log=True)
            try:
                visible, dlq_depth = self._read_queue_depths(resource_id)
            except Exception as exc:
                last = "The ticket stays open because the queue could not be read. " + public_text(exc)
                self._note_issue(key, "Verified action ran. " + last)
                return last, False
            if visible == 0 and dlq_depth == 0:
                closed = self._close_issue(key)
                return closed, bool(key) and closed.startswith(f"{key} was closed")
            last = (
                f"Visible messages are {visible} and the dead-letter depth is {dlq_depth}. "
                "The ticket stays open because the queue is not empty."
            )
            if attempt >= attempts:
                self._note_issue(key, "Verified action ran. " + last)
                return last, False
            self.set_phase("Waiting a few seconds to read the lab queue again.", log=True)
            time.sleep(pause)
        return last, False

    def _read_queue_depths(self, resource_id: str) -> tuple[int, int]:
        """Visible messages on this queue and the depth of its dead-letter queue."""
        if "sre-agent" in (resource_id or "").lower():
            raise RuntimeError("Refusing to read an sre-agent queue.")
        refresh_aws_login()
        import boto3

        region, url = _sqs_target(resource_id)
        client = boto3.Session(region_name=region).client("sqs", region_name=region)
        attrs = client.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "RedrivePolicy"],
        )["Attributes"]
        visible = int(attrs.get("ApproximateNumberOfMessages") or 0)
        policy = attrs.get("RedrivePolicy")
        dlq_arn = ""
        if policy:
            dlq_arn = str(json.loads(policy).get("deadLetterTargetArn") or "")
        if "sre-agent" in dlq_arn.lower():
            raise RuntimeError("Refusing to read an sre-agent dead-letter queue.")
        if not dlq_arn:
            return visible, 0
        _dlq_region, dlq_url = _sqs_target(dlq_arn)
        if _dlq_region == region:
            dlq_client = client
        else:
            dlq_client = boto3.Session(region_name=_dlq_region).client("sqs", region_name=_dlq_region)
        dlq_attrs = dlq_client.get_queue_attributes(
            QueueUrl=dlq_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )["Attributes"]
        return visible, int(dlq_attrs.get("ApproximateNumberOfMessages") or 0)

    def _run_lab_redrive(self, resource_id: str, name: str) -> str:
        from sre_agent.demo_plans import explicit_lab_target
        from sre_agent.diagnosis.rules import order_outcomes
        from sre_agent.discovery.service import _reader_for

        targets = lab_target_ids()
        if not explicit_lab_target(resource_id, name, targets):
            return NOT_RUN
        agent = getattr(self, "agent", None)
        registry = getattr(agent, "registry", None)
        record = registry.get_by_resource_id(resource_id) if registry is not None else None
        if record is None and registry is not None:
            wanted = resource_id.rsplit(":", 1)[-1]
            for item in registry.list_resources():
                if item.resource_id == resource_id or item.name == wanted or item.name == name:
                    record = item
                    break
        if record is None or not explicit_lab_target(record.resource_id, record.name, targets):
            return NOT_RUN
        self.set_phase("Checking the lab queue before sqs.redrive_dlq.", log=True)
        tools = agent.tools
        tools.inspect(record.resource_id, "demo-ticket")
        snapshot = tools.snapshot()
        lines: list[str] = []
        for read in snapshot.logs.values():
            lines.extend(getattr(read, "lines", []) or [])
        eligible, _rejected, _notes = order_outcomes(list(getattr(snapshot, "dlq_messages", []) or []), lines)
        policy = (record.config or {}).get("redrive_policy") or {}
        dlq = str(policy.get("deadLetterTargetArn") or "")
        if not dlq:
            return "sqs.redrive_dlq did not run. The queue has no dead-letter target."
        if "sre-agent" in dlq.lower():
            return NOT_RUN
        reader = _reader_for(agent.scope, record.account_id)
        if reader is None:
            return NOT_RUN
        if eligible and hasattr(reader, "replay_orders"):
            result = reader.replay_orders(record.account_id, record.region, record.resource_id, dlq, eligible)
            return f"sqs.redrive_dlq ran on the lab target. Moved {result.get('moved', 0)}."
        if not hasattr(reader, "start_message_move_task"):
            return NOT_RUN
        try:
            result = reader.start_message_move_task(record.account_id, record.region, record.resource_id, dlq)
        except Exception as exc:
            return "sqs.redrive_dlq did not run. " + public_text(exc)
        status = str((result or {}).get("status") or "started")
        return f"sqs.redrive_dlq ran on the lab target. Move task {status}."

    def prepare_plan(self, action: str, payload: dict) -> dict:
        if action == "ticket":
            return self._ticket_plan(str(payload.get("issue") or ""), str(payload.get("proposed") or ""))
        return {"ok": False, "applied": False, "error": "Unknown action."}

    def _ticket_plan(self, key: str, proposed: str = "") -> dict:
        if not key:
            return {"ok": False, "applied": False, "error": "Choose an issue."}
        view = self._issue_view(key)
        if view is None:
            return {"ok": False, "applied": False, "error": f"{key} is not an open issue in this project."}
        view = {**view, "proposed": _posted_proposal(view, proposed)}
        return ticket_plan(key, _decision_for(view), REGION, proposed=str(view.get("proposed") or ""))

    def populate_workload(self, fields: dict) -> None:
        del fields
        raise RuntimeError("This page does not change Confluence.")

    def _read_catalog(self) -> None:
        """Read the stored catalog for the page. This does not write Confluence."""
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        sections, error = read_catalog_sections()
        self.catalog_sections = sections
        self.catalog_read_error = error

    def _issue_view(self, key: str) -> dict | None:
        views, _error = self.issues_for_page()
        for view in views:
            if view.get("key") == key:
                linked = _linked_issue(view, self.rows)
                linked["proposed"] = resolved_proposal(linked, str(linked.get("proposed") or ""))
                return linked
        return None

    def issues_for_page(self) -> tuple[list[dict], str]:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return list(self.issue_views), self.jira_error
        try:
            views = self._fetch_issues()
        except Exception as exc:
            self.jira_error = public_text(exc)
            return [], self.jira_error
        self.issue_views = views
        self.jira_error = ""
        return views, ""

    def _fetch_issues(self) -> list[dict]:
        from sre_agent.integrations.jira import _flatten, parse_star_description
        from sre_agent.worker import _jira

        client = _jira()
        if client is None or not hasattr(client, "list_open"):
            raise RuntimeError("Jira is not configured.")
        views = []
        for item in client.list_open():
            key = str(item.get("key") or "")
            fields = item.get("fields") or {}
            summary = str(fields.get("summary") or "")
            status_field = fields.get("status") or {}
            status = str(status_field.get("name") or "") if isinstance(status_field, dict) else ""
            description = fields.get("description")
            parsed = parse_star_description(description)
            if parsed is None:
                situation = summary
                where_seen = ""
                proposed = _flatten(description).strip()
            else:
                situation = parsed.get("issue") or parsed.get("situation") or summary
                where_seen = parsed.get("where_seen") or parsed.get("task") or ""
                proposed = parsed.get("proposed") or ""
            linked = self.issue_links.get(key, {})
            views.append(
                {
                    "key": key,
                    "summary": summary,
                    "status": status,
                    "situation": situation,
                    "where_seen": where_seen,
                    "task": where_seen,
                    "proposed": proposed,
                    "url": _browse(key),
                    "resource_id": linked.get("target") or linked.get("resource_id") or "",
                    "resource_name": linked.get("name") or "",
                    "resource_type": linked.get("resource_type") or "",
                    "note": self.ticket_notes.get(key, ""),
                }
            )
        return views

    def _apply(self, report, agent) -> None:
        by_id = {record.resource_id: record for record in report.resources}
        codes: dict[str, list[str]] = {}
        problems: dict[str, str] = {}
        links: dict[str, dict] = {}
        for diagnosis in report.diagnoses:
            if diagnosis.problem_code:
                codes.setdefault(diagnosis.resource_id, []).append(diagnosis.problem_code)
            if diagnosis.observed_problem:
                problems.setdefault(diagnosis.resource_id, diagnosis.observed_problem)
            record = by_id.get(diagnosis.resource_id)
            if record is not None and record.name == DIRECTORY and diagnosis.jira_issue_key:
                self.directory_issue_key = diagnosis.jira_issue_key
            if diagnosis.jira_issue_key and record is not None:
                action = diagnosis.proposed_action
                candidate = {
                    "resource_id": diagnosis.resource_id,
                    "name": record.name,
                    "resource_type": record.resource_type,
                    "target": action.target_resource_id if action is not None else diagnosis.resource_id,
                }
                current = links.get(diagnosis.jira_issue_key)
                if current is None or not _directory_link(current) or _directory_link(candidate):
                    links[diagnosis.jira_issue_key] = candidate
        self.issue_links = links
        rows = []
        for record in sorted(report.resources, key=lambda item: (item.resource_type, item.name.lower())):
            rows.append(
                {
                    "name": record.name,
                    "resource_id": record.resource_id,
                    "resource_type": record.resource_type,
                    "health": record.health,
                    "problem_code": _problem_code(record, codes),
                    "problem": problems.get(record.resource_id) or _fallback_problem(record),
                    "reason": record.health_reasons[0] if record.health_reasons else "",
                    "discovery_source": record.discovery_source,
                    "presence": record.presence,
                }
            )
        notes = []
        scanned = []
        for scan in report.scans:
            if scan.scanned_at:
                scanned.append(scan.scanned_at)
            if scan.collector == "reconciliation":
                continue
            if scan.status == "note":
                notes.append(public_text(scan.message))
            elif scan.status in {"error", "denied", "partial"}:
                notes.append(f"{scan.collector} ({scan.region}) {scan.status}: {public_text(scan.message)}")
        self.report = report
        self.rows = rows
        self.scan_notes = notes
        self.scanned_at = max(scanned) if scanned else ""
        self.catalog_status = agent.catalog_status
        self.catalog_types = sorted({rule.resource_type for rule in agent.catalog_rules})

    def _apply_directory_restore(self, function_name: str, table_name: str, issue_key: str) -> tuple[str, bool]:
        """Run the proposed TABLE_NAME change, then close after that value is read back."""
        if function_name.startswith("sre-agent") or function_name == "enterprise-sites":
            raise RuntimeError("Refusing to change " + function_name)
        allowed = {(DIRECTORY, LIVE_TABLE), ("sre-dir-lab", "sre-dir-lab")}
        if (function_name, table_name) not in allowed:
            raise RuntimeError(NOT_RUNNABLE)
        if issue_key:
            self.directory_issue_key = issue_key
        key = issue_key or self.directory_issue_key
        self.directory_changed = False
        self.mail_report = None
        self.close_mailed = False
        before = ""
        try:
            before = self._read_function_table(function_name)
        except Exception as exc:
            self.set_phase("Could not read TABLE_NAME before the update. " + public_text(str(exc)), log=True)
        self.set_phase(
            f"TABLE_NAME was {before or 'unset'}. Setting {function_name} TABLE_NAME to {table_name}.",
            log=True,
        )
        self._set_directory_table(table_name, function_name=function_name)
        self.directory_changed = True
        self.set_phase(f"Reading TABLE_NAME on {function_name}.", log=True)
        actual = self._read_function_table(function_name)
        if actual != table_name:
            stayed = f"TABLE_NAME was {before or 'unset'}. TABLE_NAME is {actual or 'unset'}. The ticket stays open because TABLE_NAME is not {table_name}."
            self._remember_directory_mail(key, function_name, before, actual or "unset", "The function was not invoked.", "The ticket stays open.")
            self._note_issue(key, "Verified action ran. " + stayed)
            return stayed, False
        failure = self._invoke_function(function_name)
        if failure:
            stayed = (
                f"TABLE_NAME was {before or 'unset'}. TABLE_NAME is {table_name}. {failure} "
                "The ticket stays open because the function invoke failed."
            )
            self._remember_directory_mail(key, function_name, before, table_name, failure, "The ticket stays open.")
            self._note_issue(key, "Verified action ran. " + stayed)
            return stayed, False
        alarm_note = self._set_lab_alarm_ok(function_name) if function_name == "sre-dir-lab" else ""
        if function_name == "sre-dir-lab" and " is OK." not in alarm_note:
            stayed = (
                f"TABLE_NAME was {before or 'unset'}. TABLE_NAME is {table_name}. "
                f"The function was invoked and returned success. {alarm_note} "
                "The ticket stays open because the CloudWatch alarm is still ALARM."
            )
            self._remember_directory_mail(key, function_name, before, table_name, alarm_note, "The ticket stays open.")
            self._note_issue(key, "Verified action ran. " + stayed)
            return stayed, False
        done = f"The function was invoked and returned success. {alarm_note}".strip()
        self._remember_directory_mail(key, function_name, before, table_name, done, f"{key} was closed because health is healthy.")
        closed = self._close_issue(key)
        did_close = bool(key) and closed.startswith(f"{key} was closed")
        if did_close:
            self.directory_issue_key = ""
        return (
            f"TABLE_NAME was {before or 'unset'}. TABLE_NAME is {table_name}. "
            f"{done} {closed}"
        ), did_close

    def _remember_directory_mail(self, key: str, function_name: str, before: str, after: str, action: str, ticket: str) -> None:
        """The mail for this Verify click: what happened, why, what changed, and the delta."""
        previous = before or "unset"
        if previous == after:
            cause = (
                f"TABLE_NAME on {function_name} was already {after}. "
                "CloudWatch alarm sre-dir-lab-lambda-errors was still ALARM from earlier Lambda errors."
            )
        else:
            cause = (
                f"TABLE_NAME on {function_name} was {previous}. "
                "The function could not read the directory table, so Lambda errors put the alarm in ALARM."
            )
        alarm_after = "OK" if " is OK." in action else "still ALARM"
        ticket_after = "closed" if "was closed" in ticket else "still open"
        self.mail_report = {
            "happened": (
                f"CloudWatch alarm sre-dir-lab-lambda-errors was in ALARM. "
                f"The checker showed {function_name} as unhealthy and opened {key or 'a Jira ticket'}."
            ),
            "cause": cause,
            "action": f"Set TABLE_NAME on {function_name} to {after}. {action} {ticket}".strip(),
            "delta": (
                f"TABLE_NAME: {previous} -> {after}\n"
                f"Alarm sre-dir-lab-lambda-errors: ALARM -> {alarm_after}\n"
                f"Jira {key or 'ticket'}: open -> {ticket_after}"
            ),
        }

    def _read_function_table(self, function_name: str) -> str:
        """The TABLE_NAME env var on this function after the configuration update."""
        if not function_name or function_name.startswith("sre-agent"):
            raise RuntimeError("Refusing to read " + function_name)
        refresh_aws_login()
        import boto3

        client = boto3.Session(region_name=REGION).client("lambda", region_name=REGION)
        config = client.get_function_configuration(FunctionName=function_name)
        variables = (config.get("Environment") or {}).get("Variables") or {}
        return str(variables.get("TABLE_NAME") or "")

    def _invoke_function(self, function_name: str) -> str:
        """Invoke the function. An empty string means AWS returned success."""
        if function_name.startswith("sre-agent"):
            raise RuntimeError("Refusing to invoke " + function_name)
        refresh_aws_login()
        import boto3

        client = boto3.Session(region_name=REGION).client("lambda", region_name=REGION)
        self.set_phase(f"Invoking {function_name}.", log=True)
        response = client.invoke(FunctionName=function_name, InvocationType="RequestResponse", Payload=b"{}")
        if response.get("FunctionError"):
            return _invoke_failure(response) or "The invoke failed."
        status = int(response.get("StatusCode") or 0)
        if status >= 300:
            return f"The invoke returned status {status}."
        return ""

    def _set_lab_alarm_ok(self, function_name: str) -> str:
        """Leave the lab alarm OK only when CloudWatch agrees and the error window is clear."""
        if function_name != "sre-dir-lab":
            return ""
        refresh_aws_login()
        import boto3

        name = "sre-dir-lab-lambda-errors"
        client = boto3.Session(region_name=REGION).client("cloudwatch", region_name=REGION)
        reason = "The directory function was invoked successfully after TABLE_NAME was set."
        deadline = time.monotonic() + 120
        last = ""
        try:
            while True:
                client.set_alarm_state(AlarmName=name, StateValue="OK", StateReason=reason)
                last = _alarm_state(client, name)
                errors = _recent_lambda_errors(client, function_name)
                if last == "OK" and (errors is None or errors < 1):
                    return f"CloudWatch alarm {name} is OK."
                if time.monotonic() >= deadline:
                    break
                time.sleep(5)
            if last == "OK":
                errors = _recent_lambda_errors(client, function_name)
                if errors is None or errors < 1:
                    return f"CloudWatch alarm {name} is OK."
        except Exception as exc:
            if "ResourceNotFound" in public_text(str(exc)):
                return ""
            return "The CloudWatch alarm was not changed. " + public_text(str(exc))
        shown = last or "ALARM"
        return f"CloudWatch alarm {name} is still {shown}."

    def _set_directory_table(self, table_name: str, function_name: str = "") -> None:
        function_name = function_name or DIRECTORY
        allowed = {
            (DIRECTORY, LIVE_TABLE),
            (DIRECTORY, "missing-table"),
            ("sre-dir-lab", "sre-dir-lab"),
        }
        if (function_name, table_name) not in allowed or function_name.startswith("sre-agent"):
            raise RuntimeError("Refusing to set an unexpected table name.")
        refresh_aws_login()
        import boto3

        client = boto3.Session(region_name=REGION).client("lambda", region_name=REGION)

        def _write() -> None:
            current = client.get_function_configuration(FunctionName=function_name)
            variables = dict((current.get("Environment") or {}).get("Variables") or {})
            variables["TABLE_NAME"] = table_name
            client.update_function_configuration(
                FunctionName=function_name,
                Environment={"Variables": variables},
            )

        try:
            try:
                _write()
            except client.exceptions.ResourceNotFoundException as exc:
                raise RuntimeError(DIRECTORY_ABSENT) from exc
            except client.exceptions.ResourceConflictException:
                _wait_function_ready(client, function_name)
                _write()
            _wait_function_ready(client, function_name)
        except Exception as exc:
            if _directory_waiter_error(exc):
                raise RuntimeError(DIRECTORY_ABSENT) from None
            raise

    def _directory_health(self) -> tuple[str, str]:
        for row in self.rows:
            if row["name"] == DIRECTORY and row["resource_type"] == "lambda:function":
                return row["health"], row["problem_code"]
        return "", ""

    def _topic_finding(self) -> str:
        row = _topic_row(self.rows)
        if self.catalog_status == "failed":
            detail = next((note for note in self.scan_notes if note.startswith("confluence")), "")
            return (
                "Created SNS topic demo-alerts (tagged Name=demo-alerts). "
                "The Confluence catalog did not load, so this scan did not treat the topic as discovered. "
                + detail
            ).strip()
        if "sns:topic" not in self.catalog_types:
            return (
                "Created SNS topic demo-alerts (tagged Name=demo-alerts). "
                "The heading Service: SNS (sns:topic) is not on the Confluence catalog page yet, "
                "so this scan did not discover the topic. Paste the table, then press the button again."
            )
        if row is None:
            return (
                "The heading Service: SNS (sns:topic) is on the page. "
                "demo-alerts was not in this inventory. Discovery source service-list did not return it."
            )
        source = row["discovery_source"] or "unknown"
        if source == "service-list":
            return "Discovered demo-alerts. Discovery source: service-list."
        return f"demo-alerts is in the inventory. Discovery source: {source}. Expected service-list."

    def _wait(self, *, minimum: int, maximum: int, label: str, ready) -> bool:
        started = time.monotonic()
        found = False
        next_probe = 0.0
        last_log = -15.0
        probe_error = ""
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= next_probe:
                try:
                    found = bool(ready()) or found
                    probe_error = ""
                except Exception as exc:
                    probe_error = public_text(exc)
                next_probe = elapsed + 5
            text = f"{label} ({int(elapsed)}s of about {minimum}-{maximum}s)."
            if found and elapsed < minimum:
                text = f"{label} The metric is visible. Holding until {minimum}s ({int(elapsed)}s)."
            if probe_error:
                text += " Last metric check failed: " + probe_error
            self.set_phase(text, log=elapsed - last_log >= 15)
            if elapsed - last_log >= 15:
                last_log = elapsed
            if found and elapsed >= minimum:
                return True
            if elapsed >= maximum:
                return found
            time.sleep(1)


def read_catalog_sections() -> tuple[list[dict], str]:
    """Fetch the catalog page and parse the sections already stored there."""
    from sre_agent.worker import _confluence

    client = _confluence()
    if client is None or not hasattr(client, "fetch_page"):
        return [], "Confluence is not configured."
    try:
        _version, storage = client.fetch_page()
        return catalog_sections(storage), ""
    except Exception as exc:
        return [], public_text(exc)


def _catalog_skip_note(note: str) -> bool:
    """Unevaluable catalog rules and a failed catalog fetch stay off the page."""
    lowered = (note or "").lower()
    if "was not evaluated" in lowered or "resourcenotfoundexception" in lowered:
        return True
    if "catalog load failed" in lowered or "http error 404" in lowered:
        return True
    return "catalog rule" in lowered and ("could not be read" in lowered or "log group" in lowered or "${" in note)


def _shown_scan_notes(notes: list[str]) -> list[str]:
    return [note for note in notes if not _catalog_skip_note(note)]


_SERVICE_NAMES = {
    "sqs": "SQS",
    "lambda": "Lambda",
    "dynamodb": "DynamoDB",
    "api gateway": "API Gateway",
    "sns": "SNS",
    "eks": "EKS",
    "ec2": "EC2",
}
_COMPARE_WORDS = {
    ">": "above",
    ">=": "at or above",
    "<": "below",
    "GreaterThanThreshold": "above",
    "GreaterThanOrEqualToThreshold": "at or above",
    "LessThanThreshold": "below",
}


def catalog_service_name(heading: str) -> str:
    """The service a person recognizes. The stored heading is not the title."""
    text = (heading or "").strip()
    if text.lower().startswith("service:"):
        text = text.split(":", 1)[1].strip()
    name = text
    resource_type = ""
    match = re.fullmatch(r"(.+?)\s*\(([a-z0-9]+:[a-z0-9_-]+)\)", text, flags=re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        resource_type = match.group(2).lower()
    elif re.fullmatch(r"[a-z0-9]+:[a-z0-9_-]+", text, flags=re.IGNORECASE):
        resource_type = text.lower()
        name = ""
    if resource_type in TYPE_LABELS:
        return TYPE_LABELS[resource_type]
    return _SERVICE_NAMES.get(name.lower(), name)


def _usable(value: str) -> str:
    text = (value or "").strip()
    if not text or "${" in text or _catalog_skip_note(text):
        return ""
    return text


def _metric_why(item: dict) -> str:
    """Plain language from the condition, threshold, and role. Empty parts are left out."""
    measure = _usable(str(item.get("measure") or ""))
    condition = _usable(str(item.get("condition") or ""))
    threshold = _usable(str(item.get("threshold") or ""))
    role = _usable(str(item.get("role") or "")).lower()
    word = _COMPARE_WORDS.get(condition, "")
    if measure and word and threshold:
        if role == "supporting":
            return f"Supporting signal. Degraded when {measure} is {word} {threshold}."
        return f"Unhealthy when {measure} is {word} {threshold}."
    if role == "supporting":
        return "Supporting signal."
    return ""


def _metric_row_html(item: dict) -> str:
    measure = _usable(str(item.get("measure") or ""))
    symptom = _usable(str(item.get("symptom") or ""))
    if not measure or not symptom:
        return ""
    why = _metric_why(item)
    return (
        "<tr>"
        f"<td>{html.escape(measure)}</td>"
        f"<td>{html.escape(why)}</td>"
        f"<td>{html.escape(symptom)}</td>"
        "</tr>"
    )


def _catalog_view_html(sections: list[dict], error: str) -> str:
    """One card per catalog service. Opening a card shows that service's metrics."""
    cards = []
    for section in sections or []:
        heading = str(section.get("heading") or "").strip()
        if not heading or _catalog_skip_note(heading) or "${" in heading:
            continue
        label = catalog_service_name(heading)
        if not label:
            continue
        body_rows = []
        for item in section.get("rows") or []:
            row = _metric_row_html(item)
            if row:
                body_rows.append(row)
        count = len(body_rows)
        count_label = "1 metric" if count == 1 else f"{count} metrics"
        if body_rows:
            table = (
                "<table><thead><tr><th>Metric</th><th>Why</th><th>Symptom ID</th></tr></thead>"
                f"<tbody>{''.join(body_rows)}</tbody></table>"
            )
        else:
            table = "<p class='meta'>No metrics are listed for this service.</p>"
        cards.append(
            "<details class='catalog-card'>"
            "<summary class='catalog-toggle'>"
            f"<span class='catalog-name'>{html.escape(label)}</span>"
            f"<span class='catalog-count'>{count_label}</span>"
            "</summary>"
            f"<div class='catalog-detail'>{table}</div>"
            "</details>"
        )
    if cards:
        return "<div class='catalog-grid'>" + "".join(cards) + "</div>"
    if error and not _catalog_skip_note(error):
        return f"<p class='meta'>{html.escape(error)}</p>"
    if error:
        return ""
    return "<p class='meta'>No service sections are on the Confluence page.</p>"


def shown_health(value: str) -> str:
    """Health words the page may show. The word unknown is never one of them."""
    text = (value or "").strip().lower()
    if text == "healthy":
        return "healthy"
    if text == "unhealthy":
        return "unhealthy"
    return "degraded"


def _problem_code(record, codes: dict) -> str:
    listed = ", ".join(dict.fromkeys(codes.get(record.resource_id, [])))
    if listed and listed != "healthy":
        return listed
    if record.monitoring_status == "unsupported":
        return "monitoring.unsupported"
    return "" if listed == "healthy" else listed


def _fallback_problem(record) -> str:
    if record.monitoring_status == "unsupported":
        return "No monitoring pack is registered for this resource type."
    if record.health_reasons:
        return record.health_reasons[0]
    return ""


def _moved_count(note: str) -> int:
    match = re.search(r"\bMoved (\d+)\b", note or "")
    if match is None:
        return 0
    return int(match.group(1))


def _stay_open(health: str, code: str) -> str:
    state = shown_health(health) if health else "degraded"
    text = f"Health is {state}."
    if code and code != "healthy":
        text += f" Problem code {code}."
    return text + " The ticket stays open because health is not healthy."


def _posted_proposal(view: dict, proposed: str) -> str:
    text = (proposed or "").strip()
    if text and not is_instruction(text):
        return text
    return resolved_proposal(view, text)


def _decision_for(view: dict) -> dict:
    return ticket_decision(
        str(view.get("proposed") or ""),
        summary=str(view.get("summary") or ""),
        resource_name=str(view.get("resource_name") or ""),
        resource_id=str(view.get("resource_id") or ""),
        resource_type=str(view.get("resource_type") or ""),
        lab_targets=lab_target_ids(),
        where_seen=str(view.get("where_seen") or view.get("task") or ""),
    )


def _directory_link(link: dict) -> bool:
    """A directory restore belongs to the function, not another resource that shares its name."""
    return (
        str(link.get("resource_type") or "") == "lambda:function"
        and str(link.get("name") or "") in {"enterprise-sites-directory", "sre-dir-lab"}
    )


def _linked_issue(view: dict, rows: list[dict]) -> dict:
    """Fill the resource from the card text when the page did not write the Jira link."""
    linked = dict(view)
    wanted = directory_restore_function(str(linked.get("proposed") or ""))
    if wanted:
        for row in rows:
            if row.get("name") == wanted and str(row.get("resource_type") or "") in {"", "lambda:function"}:
                linked["resource_id"] = str(row.get("resource_id") or "")
                linked["resource_name"] = str(row.get("name") or wanted)
                linked["resource_type"] = str(row.get("resource_type") or "lambda:function")
                return linked
        current_type = str(linked.get("resource_type") or "")
        current_name = str(linked.get("resource_name") or "")
        if current_type in {"", "lambda:function"} and current_name in {"", wanted}:
            if not current_name:
                linked["resource_name"] = wanted
            if not current_type:
                linked["resource_type"] = "lambda:function"
            return linked
        linked["resource_id"] = ""
        linked["resource_name"] = wanted
        linked["resource_type"] = "lambda:function"
        return linked
    if linked.get("resource_id") and linked.get("resource_name") and linked.get("resource_type"):
        return linked
    blob = "\n".join(
        str(linked.get(name) or "")
        for name in ("where_seen", "task", "summary", "situation", "proposed")
    )
    matches = [row for row in rows if _row_mentioned(row, blob)]
    chosen = _choose_issue_row(matches, str(linked.get("proposed") or ""))
    if chosen is None:
        return linked
    linked["resource_id"] = linked.get("resource_id") or chosen.get("resource_id") or ""
    linked["resource_name"] = linked.get("resource_name") or chosen.get("name") or ""
    linked["resource_type"] = linked.get("resource_type") or chosen.get("resource_type") or ""
    return linked


def _row_mentioned(row: dict, blob: str) -> bool:
    resource_id = str(row.get("resource_id") or "")
    name = str(row.get("name") or "")
    if resource_id and resource_id in blob:
        return True
    if name and re.search(rf"(?:^|[\s|]){re.escape(name)}(?:$|[\s|])", blob):
        return True
    return False


def _choose_issue_row(matches: list[dict], proposed: str) -> dict | None:
    if not matches:
        return None
    if "sqs.redrive_dlq" in proposed:
        targets = lab_target_ids()
        for row in matches:
            if explicit_lab_target(str(row.get("resource_id") or ""), str(row.get("name") or ""), targets):
                return row
    table_match = re.search(r"TABLE_NAME\s*(=|to)\s*(enterprise-sites|sre-dir-lab)\b", proposed, re.IGNORECASE)
    if table_match:
        wanted = "enterprise-sites-directory" if table_match.group(2).lower() == "enterprise-sites" else "sre-dir-lab"
        for row in matches:
            if row.get("name") == wanted and row.get("resource_type") in {None, "", "lambda:function"}:
                return row
    return matches[0]


def _browse(key: str) -> str:
    base = get("JIRA_BASE_URL").rstrip("/")
    if base and key:
        return f"{base}/browse/{key}"
    return _jira_url(key)


def _row_cells(row: dict) -> str:
    health = shown_health(row.get("health") or "")
    code = row.get("problem_code") or ""
    if code == "healthy":
        code = ""
    detail = code
    problem = row.get("problem") or ""
    if code and problem and not _catalog_skip_note(problem):
        detail = code + " " + problem
    return (
        f"<td>{html.escape(row['name'])}</td>"
        + f"<td>{html.escape(row['resource_type'])}</td>"
        + f"<td><span class='health {html.escape(health)}'>{html.escape(health)}</span></td>"
        + f"<td>{html.escape(detail)}</td>"
        + f"<td>{html.escape(row['discovery_source'])}</td>"
    )


def _groups_html(groups: list[dict]) -> str:
    if not groups:
        return "<p>No workloads were in this inventory.</p>"
    blocks = []
    for group in groups:
        body = "\n".join(f"<tr>{_row_cells(row)}</tr>" for row in group["rows"])
        label = html.escape(group["label"])
        count = len(group["rows"])
        resource_type = html.escape(group["resource_type"])
        blocks.append(
            "<div class='type-group'>"
            f"<button type='button' class='type-toggle stat' aria-expanded='false' data-type='{resource_type}'>"
            f"<span class='type-label'>{label}</span><span class='type-count'>{count}</span>"
            "</button>"
            "<div class='type-detail' hidden>"
            "<table><thead><tr><th>Name</th><th>Type</th><th>Health</th><th>Problem code</th><th>Source</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div></div>"
        )
    return "<div class='inventory-grid'>\n" + "\n".join(blocks) + "\n</div>"


def _issue_cards(issues: list[dict], error: str) -> str:
    if error:
        return f"<p class='result'>{html.escape(error)}</p>"
    if not issues:
        return "<p class='meta'>No issues are on the board.</p>"
    cards = []
    for issue in issues:
        key = html.escape(issue.get("key") or "")
        url = issue.get("url") or ""
        key_html = f'<a class="key" href="{html.escape(url)}">{key}</a>' if url else f'<span class="key">{key}</span>'
        note = issue.get("note") or ""
        note_html = f"<p class='result'>{html.escape(note)}</p>" if note else ""
        proposed = html.escape(issue.get("proposed") or "")
        button = ""
        if _decision_for(issue).get("run"):
            button = f"<button type='button' class='ticket-run' data-issue='{key}'>Take this action</button>"
        cards.append(
            "<article class='card'>"
            f"<header>{key_html}<span class='status-pill'>{html.escape(issue.get('status') or 'Open')}</span></header>"
            f"<h3>Issue</h3><p class='situation'>{html.escape(issue.get('situation') or '')}</p>"
            f"<h3>Where seen</h3><p class='where'>{html.escape(issue.get('where_seen') or issue.get('task') or '')}</p>"
            f"<label>Proposed solution<textarea class='proposed'>{proposed}</textarea></label>"
            f"{note_html}"
            f"{button}"
            "</article>"
        )
    return "<div class='board'>\n" + "\n".join(cards) + "\n</div>"


def _guard_demo_resource(name: str) -> None:
    if name in {EXTRA_TABLE, TOPIC_NAME} and not name.startswith("sre-agent") and name != LIVE_TABLE:
        return
    raise RuntimeError("Refusing to change " + name)


def _topic_row(rows: list[dict]) -> dict | None:
    for row in rows:
        if row["name"] == TOPIC_NAME and row["resource_type"] == "sns:topic":
            return row
    return None


def _wait_for_topic(session) -> bool:
    client = session.client("resourcegroupstaggingapi", region_name=REGION)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        paginator = client.get_paginator("get_resources")
        for page in paginator.paginate(ResourceTypeFilters=["sns:topic"], ResourcesPerPage=100):
            for item in page.get("ResourceTagMappingList", []):
                arn = str(item.get("ResourceARN") or "")
                if arn.endswith(":" + TOPIC_NAME):
                    return True
        time.sleep(5)
    return False


def _lambda_metric(name: str, start: datetime, function_name: str = "") -> list[dict]:
    refresh_aws_login()
    import boto3

    client = boto3.Session(region_name=REGION).client("cloudwatch", region_name=REGION)
    response = client.get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName=name,
        Dimensions=[{"Name": "FunctionName", "Value": function_name or DIRECTORY}],
        StartTime=start,
        EndTime=datetime.now(timezone.utc),
        Period=60,
        Statistics=["Sum"],
    )
    return list(response.get("Datapoints") or [])


def _invoke_sites() -> list[str]:
    codes = []
    for _ in range(4):
        try:
            with urllib.request.urlopen(SITES_URL, timeout=30) as response:
                response.read(256)
                codes.append(str(response.status))
        except urllib.error.HTTPError as exc:
            exc.read(256)
            codes.append(str(exc.code))
        except urllib.error.URLError as exc:
            codes.append("failed:" + public_text(exc.reason))
    return codes


def _codes(codes: list[str]) -> str:
    return ", ".join(codes) if codes else "none"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _jira_url(key: str) -> str:
    template = get("JIRA_BROWSE_LINK")
    if not template or not key:
        return ""
    if "{key}" in template:
        return template.replace("{key}", key)
    return template.rstrip("/") + "/" + key


def _aws_console_url() -> str:
    """Console link from config only. An empty setting means the page shows no console link."""
    return get("AWS_CONSOLE_URL")


def _confluence_url() -> str:
    link = get("CONFLUENCE_PAGE_LINK")
    if link:
        return link
    base = get("CONFLUENCE_BASE_URL").rstrip("/")
    page_id = get("CONFLUENCE_PAGE_ID")
    if base and page_id:
        return f"{base}/wiki/pages/viewpage.action?pageId={page_id}"
    return ""


def _directory_waiter_error(exc: BaseException) -> bool:
    """True when a directory update hit a function that is not in this account."""
    text = str(exc).lower()
    missing = "resourcenotfoundexception" in text or "function not found" in text
    if "functionupdated" in text and missing:
        return True
    if DIRECTORY in text and missing:
        return True
    response = getattr(exc, "response", None) or getattr(exc, "last_response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        if isinstance(error, dict):
            code = str(error.get("Code") or "")
            message = str(error.get("Message") or "")
            blob = f"{text} {message}".lower()
            if code == "ResourceNotFoundException" and DIRECTORY in blob:
                return True
    return False


def _sqs_target(resource_id: str) -> tuple[str, str]:
    """Region and queue URL for an SQS ARN or URL."""
    text = (resource_id or "").strip()
    if text.startswith("https://"):
        host = text.split("/")[2]
        region = host.split(".")[1] if host.startswith("sqs.") else REGION
        return region, text
    parts = text.split(":")
    if len(parts) >= 6 and parts[2] == "sqs":
        region, account, name = parts[3], parts[4], parts[5]
        return region, f"https://sqs.{region}.amazonaws.com/{account}/{name}"
    raise RuntimeError("The queue address could not be read.")


def _wait_function_ready(client, function_name: str) -> None:
    """Poll until the configuration update is active. The first check does not sleep."""
    for attempt in range(12):
        current = client.get_function_configuration(FunctionName=function_name)
        status = str(current.get("LastUpdateStatus") or "")
        if status in {"", "Successful"}:
            return
        if status == "Failed":
            reason = str(current.get("LastUpdateStatusReason") or "The configuration update failed.")
            raise RuntimeError(public_text(reason))
        if attempt < 11:
            time.sleep(0.4)
    raise RuntimeError("The configuration update did not become active.")


def _alarm_state(client, name: str) -> str:
    response = client.describe_alarms(AlarmNames=[name])
    alarms = response.get("MetricAlarms") or []
    if not alarms:
        return ""
    return str(alarms[0].get("StateValue") or "")


def _recent_lambda_errors(client, function_name: str) -> float | None:
    """Newest one-minute Lambda error sum. None means CloudWatch has no point in the window."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=3)
    response = client.get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName="Errors",
        Dimensions=[{"Name": "FunctionName", "Value": function_name}],
        StartTime=start,
        EndTime=end,
        Period=60,
        Statistics=["Sum"],
    )
    points = list(response.get("Datapoints") or [])
    if not points:
        return None
    newest = max(points, key=lambda item: item.get("Timestamp"))
    return float(newest.get("Sum") or 0)


def _invoke_failure(response: dict) -> str:
    """A short invoke error. The payload body is not shown."""
    raw = b""
    payload = response.get("Payload")
    if payload is not None and hasattr(payload, "read"):
        raw = payload.read()
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    kind = ""
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        parsed = {}
    if isinstance(parsed, dict):
        kind = str(parsed.get("errorType") or "")
    if kind:
        return "The invoke failed: " + public_text(kind)[:120]
    return "The invoke failed."


def _function_exists(client, function_name: str) -> bool:
    if not function_name or function_name.startswith("sre-agent"):
        return False
    try:
        client.get_function(FunctionName=function_name)
    except Exception as exc:
        missing = getattr(getattr(client, "exceptions", None), "ResourceNotFoundException", ())
        if (missing and isinstance(exc, missing)) or _directory_waiter_error(exc):
            return False
        raise
    return True


def _directory_function_exists(client) -> bool:
    try:
        client.get_function(FunctionName=DIRECTORY)
    except Exception as exc:
        missing = getattr(getattr(client, "exceptions", None), "ResourceNotFoundException", ())
        if (missing and isinstance(exc, missing)) or _directory_waiter_error(exc):
            return False
        raise
    return True


def _clear_stale_directory_failure(demo: Demo) -> None:
    """Drop a stored directory waiter failure when the board has no open issues."""
    with demo._lock:
        blob = " ".join([demo.error, demo.message, demo.phase])
        if not _directory_failure_text(blob):
            return
        demo.error = ""
        if demo.message == "The last action failed." or _directory_failure_text(demo.message):
            demo.message = ""
        if demo.phase.startswith("The last action failed.") or _directory_failure_text(demo.phase):
            demo.phase = ""


def _directory_failure_text(text: str) -> bool:
    lowered = (text or "").lower()
    if "functionupdated" in lowered:
        return True
    if DIRECTORY_ABSENT.lower() in lowered:
        return True
    return DIRECTORY in lowered and ("resourcenotfoundexception" in lowered or "function not found" in lowered)


def _error_text(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = str(error.get("Code") or "")
        message = str(error.get("Message") or "")
        if code or message:
            return public_text(f"{code}: {message}".strip(": "))
    return public_text(exc)


_login_deadline = 0.0
_login_lock = threading.Lock()
_session_expires_at: datetime | None = None


def refresh_aws_login(*, force: bool = False) -> None:
    """Copy the AWS CLI login session into this process. Values are not logged.

    One export covers the whole operation. Later calls reuse it until the session is near expiry.
    """
    global _login_deadline
    with _login_lock:
        if (
            not force
            and time.monotonic() < _login_deadline
            and os.environ.get("AWS_ACCESS_KEY_ID")
            and os.environ.get("AWS_SECRET_ACCESS_KEY")
        ):
            return
        _export_aws_login()
        _login_deadline = _credential_deadline()


def _cli_login_env() -> dict[str, str]:
    """Environment for `aws configure export-credentials`.

    A copied session in this process must not be visible to that command.
    The CLI treats those variables as the credentials to export, so a later
    refresh would keep handing back the same expired token.
    """
    env = dict(os.environ)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
    ):
        env.pop(name, None)
    return env


def _aws_cli(args: list[str]) -> list[str]:
    profile = get("AWS_PROFILE")
    if profile:
        return ["aws", "--profile", profile, *args]
    return ["aws", *args]


def _export_aws_login() -> None:
    """Copy a CLI login session into this process. A dead session starts sign-in and tries once more."""
    if _apply_exported_credentials(_export_credentials()):
        return
    _start_aws_login()
    if _apply_exported_credentials(_export_credentials()):
        return
    raise RuntimeError("AWS sign-in did not finish. Reload this page to try again.")


def _export_credentials() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _aws_cli(["configure", "export-credentials", "--format", "process"]),
        capture_output=True,
        text=True,
        check=False,
        env=_cli_login_env(),
    )


def _start_aws_login() -> None:
    """Open the AWS CLI sign-in. The page waits for it instead of stopping."""
    print("AWS sign-in is starting.", flush=True)
    try:
        subprocess.run(
            _aws_cli(["login"]),
            check=False,
            env=_cli_login_env(),
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return


def _apply_exported_credentials(completed: subprocess.CompletedProcess[str]) -> bool:
    """Store a usable export. An empty or expired export is not stored."""
    global _session_expires_at
    if completed.returncode != 0 or not (completed.stdout or "").strip():
        return False
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False
    access = str(payload.get("AccessKeyId") or "")
    secret = str(payload.get("SecretAccessKey") or "")
    if not access or not secret:
        return False
    expires = _parse_expiration(str(payload.get("Expiration") or ""))
    if expires is not None and expires <= datetime.now(timezone.utc) + timedelta(seconds=30):
        return False
    os.environ["AWS_ACCESS_KEY_ID"] = access
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret
    token = str(payload.get("SessionToken") or "")
    if token:
        os.environ["AWS_SESSION_TOKEN"] = token
    else:
        os.environ.pop("AWS_SESSION_TOKEN", None)
    os.environ["AWS_DEFAULT_REGION"] = REGION
    os.environ["AWS_REGION"] = REGION
    _session_expires_at = expires
    return True


def _expired_token(exc: BaseException) -> bool:
    text = f"{_error_text(exc)} {exc}".lower()
    return "expiredtoken" in text or "security token included in the request is expired" in text


def _mail_with_login(call):
    """Refresh the AWS login, then run one SES check. Retry once if that token is already dead."""
    try:
        refresh_aws_login(force=True)
    except Exception as exc:
        return {"ok": False, "message": public_text(exc)}
    result = call()
    text = str(result.get("message") or "") if isinstance(result, dict) else ""
    if isinstance(result, dict) and result.get("ok") is False and (
        _expired_token(RuntimeError(text)) or "AWS login expired" in text
    ):
        try:
            refresh_aws_login(force=True)
        except Exception as exc:
            return {"ok": False, "message": public_text(exc)}
        result = call()
    return result


def _parse_expiration(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires.tzinfo is None:
        return expires.replace(tzinfo=timezone.utc)
    return expires.astimezone(timezone.utc)


def _credential_deadline() -> float:
    """How long the exported session can be reused. The token itself is not logged."""
    if _session_expires_at is not None:
        remaining = (_session_expires_at - datetime.now(timezone.utc)).total_seconds() - 60
        if remaining > 0:
            return time.monotonic() + remaining
    return time.monotonic() + 300


PAGE_CSS = """
:root {
  color-scheme: light;
  --ink: #1a1d21;
  --muted: #5c6570;
  --line: #d5dbe3;
  --canvas: #ffffff;
  --panel: #ffffff;
  --panel-2: #f7f8fa;
  --accent: #1a56c4;
  --green: #1e7a46;
  --green-bg: #e5f4ea;
  --red: #b42318;
  --red-bg: #fdecea;
  --amber: #8a5a00;
  --amber-bg: #fff4df;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--canvas);
  color: var(--ink);
  font: 15px/1.45 "Segoe UI", sans-serif;
}
a { color: var(--accent); }
h1 { font-size: 20px; font-weight: 650; margin: 0; }
h2 { font-size: 16px; font-weight: 650; margin: 0 0 8px; }
p { margin: 8px 0; }
button, .btn {
  font: inherit;
  color: var(--ink);
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 6px 12px;
  cursor: pointer;
  text-decoration: none;
  display: inline-block;
}
button.quiet, .btn.quiet { background: #fff; }
button:disabled { opacity: 0.45; cursor: not-allowed; }
.page-head {
  width: min(1180px, calc(100% - 28px));
  margin: 18px auto 0;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.brand h1 { margin: 0; line-height: 1.3; }
.baseline { margin: 8px 0 0; font-size: 15px; line-height: 1.45; }
.baseline.bad { color: var(--red); }
.top-actions { display: flex; gap: 8px; align-items: center; flex: none; }
.wrap, main, footer { width: min(1180px, calc(100% - 28px)); margin: 0 auto; }
main { padding: 16px 0 8px; }
section.panel, section.inventory, footer section {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 14px 16px;
  margin: 0 0 12px;
}
.meta { color: var(--muted); font-size: 13px; }
.inventory-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px;
  margin-top: 10px;
}
.type-group.is-open { grid-column: 1 / -1; }
button.type-toggle {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: space-between;
  width: 100%;
  min-height: 92px;
  margin: 0;
  text-align: left;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 12px 14px;
}
button.type-toggle[aria-expanded="true"] { border-color: var(--accent); }
.type-label { color: var(--muted); font-size: 13px; }
.type-count { font-size: 32px; font-weight: 650; line-height: 1; }
.type-detail { margin-top: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 650; }
.health {
  display: inline-block;
  min-width: 88px;
  padding: 2px 8px;
  border-radius: 999px;
  text-align: center;
  background: var(--amber-bg);
  color: var(--amber);
}
.health.healthy { background: var(--green-bg); color: var(--green); }
.health.unhealthy { background: var(--red-bg); color: var(--red); }
.health.degraded { background: var(--amber-bg); color: var(--amber); }
.notes { background: var(--amber-bg); color: var(--amber); border-radius: 4px; padding: 8px 10px; margin-bottom: 10px; }
.catalog-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px;
  margin-top: 10px;
}
details.catalog-card {
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 12px 14px;
}
details.catalog-card[open] {
  grid-column: 1 / -1;
  background: #fff;
  border-color: var(--accent);
}
summary.catalog-toggle {
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  min-height: 72px;
  list-style: none;
}
summary.catalog-toggle::-webkit-details-marker { display: none; }
.catalog-name { font-size: 18px; font-weight: 650; line-height: 1.2; }
.catalog-count { color: var(--muted); font-size: 13px; }
.catalog-detail { margin-top: 12px; }
label { display: block; font-size: 13px; color: var(--muted); margin: 8px 0; }
.hint { display: block; font-size: 12px; }
input, select, textarea {
  display: block;
  width: 100%;
  margin-top: 4px;
  padding: 7px 8px;
  color: var(--ink);
  background: var(--canvas);
  border: 1px solid var(--line);
  border-radius: 4px;
  font: inherit;
}
textarea { min-height: 72px; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 4px 12px; }
.measure-line {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: nowrap;
  align-items: flex-end;
  gap: 12px;
}
.measure-line > label { flex: 1 1 0; min-width: 0; margin: 8px 0; }
.condition-row {
  display: flex;
  flex-wrap: nowrap;
  align-items: flex-end;
  gap: 8px;
  margin: 0 0 10px;
}
.condition-row > label { flex: 1 1 0; min-width: 0; margin: 0; }
.condition-row > button { flex: none; }
footer { padding-bottom: 28px; }
.board {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
  align-items: stretch;
}
article.card {
  height: 100%;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
article.card .ticket-run { margin-top: auto; align-self: flex-start; }
article.card header { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
.key { font-weight: 700; }
.status-pill { color: var(--muted); font-size: 12px; border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; }
.situation, .where { margin: 0; white-space: pre-wrap; }
.card h3 { font-size: 13px; font-weight: 650; margin: 8px 0 2px; }
.result { white-space: pre-wrap; background: var(--panel-2); border-radius: 4px; padding: 8px; font-size: 13px; }
#facts-overlay, #verify-overlay, #mail-verify-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  z-index: 5;
}
#facts-overlay[hidden], #verify-overlay[hidden], #mail-verify-overlay[hidden] { display: none; }
#conditions-overlay .popup { width: min(1100px, 100%); }
.popup {
  width: min(560px, 100%);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 18px;
}
.facts { display: grid; gap: 10px; margin: 12px 0; }
.facts div { background: var(--panel-2); border: 1px solid var(--line); border-radius: 4px; padding: 8px 10px; }
.facts dt { color: var(--muted); font-size: 12px; }
.facts dd { margin: 2px 0 0; font-size: 16px; }
#verify-items { white-space: pre-wrap; background: var(--panel-2); border-radius: 4px; padding: 10px; margin: 10px 0; }
.popup-actions { display: flex; justify-content: flex-end; gap: 8px; }
"""

PAGE_SCRIPT = """
const statusEl = document.getElementById("status");
const overlay = document.getElementById("verify-overlay");
const applyBtn = document.getElementById("verify-apply");
const closeBtn = document.getElementById("verify-close");
const mailEl = document.getElementById("verify-mail");
let pending = "";
let pendingBody = null;
let applied = false;
async function poll() {
  if (!statusEl) return;
  try {
    const response = await fetch("/api/status");
    const body = await response.json();
    statusEl.textContent = body.error ? (body.message + " " + body.error) : body.message;
    statusEl.className = body.error ? "baseline bad" : "baseline";
  } catch (err) {
    statusEl.textContent = "The status request failed.";
  }
}
async function openPlan(action, extra) {
  if (!overlay) return;
  pending = action;
  pendingBody = Object.assign({action: action}, extra || {});
  applied = false;
  if (mailEl) mailEl.hidden = true;
  applyBtn.hidden = false;
  applyBtn.disabled = true;
  document.getElementById("verify-title").textContent = "Check this first";
  document.getElementById("verify-lead").textContent = "Loading the plan.";
  document.getElementById("verify-items").textContent = "";
  overlay.hidden = false;
  const response = await fetch("/api/plan", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(pendingBody),
  });
  const body = await response.json();
  if (!response.ok || body.applied !== false) {
    document.getElementById("verify-lead").textContent = body.error || "The plan could not be loaded.";
    applyBtn.hidden = true;
    return;
  }
  document.getElementById("verify-title").textContent = body.title || "Check this first";
  document.getElementById("verify-lead").textContent = body.lead || "";
  document.getElementById("verify-items").textContent = (body.items || []).join("\\n");
  applyBtn.textContent = body.button || "Verify and apply";
  applyBtn.dataset.simulate = body.requires_verify ? "false" : "true";
  if (body.will_run === false) {
    applyBtn.hidden = true;
    return;
  }
  applyBtn.hidden = false;
  applyBtn.disabled = false;
  applyBtn.focus();
}
if (closeBtn) {
  closeBtn.addEventListener("click", () => {
    overlay.hidden = true;
    pending = "";
    if (applied) location.reload();
  });
}
if (applyBtn) {
  applyBtn.addEventListener("click", async () => {
    if (!pending || applyBtn.disabled) return;
    const action = pending;
    const simulate = applyBtn.dataset.simulate === "true";
    applyBtn.disabled = true;
    document.querySelectorAll("button[data-plan], button.ticket-run").forEach(button => { button.disabled = true; });
    const timer = setInterval(poll, 1000);
    try {
      const payload = Object.assign({}, pendingBody || {action: action});
      if (simulate) payload.simulate = true;
      else payload.verify = true;
      const response = await fetch("/api/verify", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      clearInterval(timer);
      const body = await response.json();
      applied = body.applied === true;
      document.getElementById("verify-lead").textContent = body.message || body.error || "";
      if (applied) {
        document.getElementById("verify-items").textContent = "";
      }
      if (mailEl) {
        mailEl.hidden = false;
        mailEl.textContent = body.mail || body.error || "";
      }
      applyBtn.hidden = true;
      if (!applied) {
        document.querySelectorAll("button[data-plan], button.ticket-run").forEach(button => { button.disabled = false; });
      }
    } catch (err) {
      clearInterval(timer);
      if (mailEl) {
        mailEl.hidden = false;
        mailEl.textContent = "The browser lost the connection to the demo server.";
      }
      applyBtn.hidden = true;
    }
  });
}
document.querySelectorAll("button[data-plan]").forEach(button => {
  button.addEventListener("click", () => {
    openPlan(button.dataset.plan, {});
  });
});
document.querySelectorAll("button.ticket-run").forEach(button => {
  button.addEventListener("click", () => {
    const card = button.closest("article");
    const field = card ? card.querySelector("textarea") : null;
    openPlan("ticket", {issue: button.dataset.issue, proposed: field ? field.value : ""});
  });
});
document.querySelectorAll(".type-toggle").forEach(button => {
  button.addEventListener("click", () => {
    const detail = button.nextElementSibling;
    const wasOpen = button.getAttribute("aria-expanded") === "true";
    document.querySelectorAll(".type-toggle").forEach(other => {
      other.setAttribute("aria-expanded", "false");
      const panel = other.nextElementSibling;
      if (panel) panel.hidden = true;
      const group = other.closest(".type-group");
      if (group) group.classList.remove("is-open");
    });
    if (!wasOpen && detail) {
      button.setAttribute("aria-expanded", "true");
      detail.hidden = false;
      const group = button.closest(".type-group");
      if (group) group.classList.add("is-open");
    }
  });
});
function fillAddresses(addresses) {
  const select = document.getElementById("saved-addresses");
  if (!select) return;
  select.replaceChildren();
  const saved = Array.isArray(addresses) ? addresses : [];
  if (!saved.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No addresses are saved";
    select.appendChild(option);
    return;
  }
  saved.forEach(address => {
    const option = document.createElement("option");
    option.value = address;
    option.textContent = address;
    select.appendChild(option);
  });
}
const saveEmails = document.getElementById("save-emails");
if (saveEmails) {
  saveEmails.addEventListener("click", async () => {
    const field = document.getElementById("new-address");
    const response = await fetch("/api/emails", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({address: field ? field.value : ""}),
    });
    const body = await response.json();
    document.getElementById("email-status").textContent = body.message || "";
    if (body.ok && Array.isArray(body.addresses)) {
      fillAddresses(body.addresses);
      const added = field ? field.value.trim() : "";
      if (field) field.value = "";
      if (added) openMailVerify(added);
    }
  });
}
function openMailVerify(address) {
  const overlay = document.getElementById("mail-verify-overlay");
  const title = document.getElementById("mail-verify-address");
  const note = document.getElementById("mail-verify-note");
  const status = document.getElementById("email-status");
  if (title) title.textContent = address;
  if (note) note.textContent = "Checking this address with Amazon…";
  if (status) status.textContent = "Checking this address with Amazon…";
  if (overlay) overlay.hidden = false;
  sendMailVerification(address);
}
async function sendMailVerification(address) {
  const note = document.getElementById("mail-verify-note");
  const status = document.getElementById("email-status");
  const response = await fetch("/api/emails", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({verify: address}),
  });
  const body = await response.json();
  const message = body.message || "";
  if (note) note.textContent = message;
  if (status) status.textContent = message;
}
const mailVerifySend = document.getElementById("mail-verify-send");
if (mailVerifySend) {
  mailVerifySend.addEventListener("click", () => {
    const address = document.getElementById("mail-verify-address");
    if (address && address.textContent) sendMailVerification(address.textContent);
  });
}
const mailVerifyCheck = document.getElementById("mail-verify-check");
if (mailVerifyCheck) {
  mailVerifyCheck.addEventListener("click", async () => {
    const address = document.getElementById("mail-verify-address");
    const note = document.getElementById("mail-verify-note");
    if (!address || !address.textContent) return;
    const response = await fetch("/api/emails", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({check: address.textContent}),
    });
    const body = await response.json();
    const message = body.message || "";
    if (note) note.textContent = message;
    const status = document.getElementById("email-status");
    if (status) status.textContent = message;
  });
}
const mailVerifyClose = document.getElementById("mail-verify-close");
if (mailVerifyClose) {
  mailVerifyClose.addEventListener("click", () => {
    const overlay = document.getElementById("mail-verify-overlay");
    if (overlay) overlay.hidden = true;
  });
}
const verifyEmail = document.getElementById("verify-email");
if (verifyEmail) {
  verifyEmail.addEventListener("click", () => {
    const select = document.getElementById("saved-addresses");
    const selected = select ? select.value : "";
    const status = document.getElementById("email-status");
    if (!selected) {
      if (status) status.textContent = "No addresses are saved.";
      return;
    }
    openMailVerify(selected);
  });
}
const deleteEmail = document.getElementById("delete-email");
if (deleteEmail) {
  deleteEmail.addEventListener("click", async () => {
    const select = document.getElementById("saved-addresses");
    const selected = select ? select.value : "";
    const status = document.getElementById("email-status");
    if (!selected) {
      if (status) status.textContent = "No addresses are saved.";
      return;
    }
    const response = await fetch("/api/emails", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({delete: selected}),
    });
    const body = await response.json();
    if (status) status.textContent = body.message || "";
    if (body.ok && Array.isArray(body.addresses)) fillAddresses(body.addresses);
  });
}
const factsOpen = document.getElementById("facts-open");
const facts = document.getElementById("facts-overlay");
if (factsOpen && facts) {
  factsOpen.addEventListener("click", () => { facts.hidden = false; });
  document.getElementById("facts-close").addEventListener("click", () => { facts.hidden = true; });
}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>" + html.escape(title) + "</title>\n<style>\n"
        + PAGE_CSS
        + "\n</style>\n</head>\n<body>\n"
        + body
        + "\n<script>\n"
        + PAGE_SCRIPT
        + "\n</script>\n</body>\n</html>\n"
    )


def _banner(demo: Demo) -> tuple[str, str]:
    banner_class = "baseline bad" if demo.error else "baseline"
    banner = demo.error if demo.error and not demo.message else (demo.message or demo.phase)
    if demo.error and demo.message:
        banner = demo.message + " " + demo.error
    return banner_class, banner


def _verify_overlay() -> str:
    return """
<div id="verify-overlay" hidden>
  <div class="popup" role="dialog" aria-modal="true" aria-labelledby="verify-title">
    <h2 id="verify-title">Check this first</h2>
    <p id="verify-lead"></p>
    <div id="verify-items"></div>
    <p id="verify-mail" class="result" hidden></p>
    <div class="popup-actions">
      <button type="button" id="verify-close" class="quiet">Close</button>
      <button type="button" id="verify-apply">Verify and apply</button>
    </div>
  </div>
</div>
"""


def _address_options(addresses: list[str]) -> str:
    if not addresses:
        return '<option value="">No addresses are saved</option>'
    return "".join(
        f'<option value="{html.escape(item, quote=True)}">{html.escape(item)}</option>'
        for item in addresses
    )


def render(demo: Demo) -> str:
    banner_class, banner = _banner(demo)
    catalog_link = _confluence_url()
    page_id = get("CONFLUENCE_PAGE_ID")
    page_name = html.escape(f"Confluence page {page_id}" if page_id else "the Confluence page")
    catalog_html = (
        f'<a href="{html.escape(catalog_link)}">{page_name}</a>'
        if catalog_link
        else page_name
    )
    groups = inventory_groups(demo.rows)
    groups_html = _groups_html(groups)
    workload_count = sum(len(group["rows"]) for group in groups)
    visible_notes = _shown_scan_notes(demo.scan_notes)
    notes = ""
    if visible_notes:
        items = "".join(f"<li>{html.escape(note)}</li>" for note in visible_notes)
        notes = f"<div class='notes'><strong>Scan notes</strong><ul>{items}</ul></div>"
    scanned = html.escape(demo.scanned_at) if demo.scanned_at else "not completed"
    saved_emails = _address_options(load_recipients())
    catalog_state = html.escape(demo.catalog_status)
    catalog_body = _catalog_view_html(demo.catalog_sections, demo.catalog_read_error)
    console = _aws_console_url()
    sites = (
        f'<p><a href="{html.escape(console, quote=True)}">{html.escape(console)}</a></p>'
        if console
        else ""
    )
    body = f"""
<header class="page-head">
  <div class="brand">
    <h1>SRE health checker</h1>
    <p class="{banner_class}" id="status">{html.escape(banner)}</p>
  </div>
  <div class="top-actions">
    <a class="btn" href="/jira">Jira</a>
    <button type="button" id="facts-open" class="quiet">Details</button>
  </div>
</header>
<main>
  <section class="inventory">
    <h2>AWS workloads in this account right now</h2>
    <p class="meta">This open read the account in this region. {workload_count} resources.</p>
    {sites}
    {notes}
    {groups_html}
  </section>
  <section class="panel" id="panel-jira">
    <h2>Jira</h2>
    <p class="meta">Open issues for this project, on their own board.</p>
    <p><a class="btn" href="/jira">Jira</a></p>
  </section>
  <section class="panel" id="panel-confluence">
    <h2>Confluence</h2>
    <p class="meta">Monitored services on the catalog page. Open a service to see its metrics and why each one is there. This view does not add or edit them.</p>
    <p>{catalog_html}</p>
    {catalog_body}
  </section>
</main>
<footer>
  <section>
    <h2>Mail</h2>
    <label for="saved-addresses">Saved addresses</label>
    <select id="saved-addresses">{saved_emails}</select>
    <label for="new-address">Add an address</label>
    <input id="new-address" name="new-address" type="text" autocomplete="off">
    <p>
      <button type="button" id="save-emails">Add address</button>
      <button type="button" id="verify-email">Verify address</button>
      <button type="button" id="delete-email">Delete address</button>
    </p>
    <p class="meta" id="ses-note">Each added address must be verified with Amazon before mail can arrive. An empty list does not send.</p>
    <p id="email-status" class="meta"></p>
  </section>
</footer>
<div id="mail-verify-overlay" hidden>
  <div class="popup" role="dialog" aria-modal="true" aria-labelledby="mail-verify-title">
    <h2 id="mail-verify-title">Verify this address</h2>
    <p id="mail-verify-address"></p>
    <p>Amazon sends a message to this address. Open it and click the verification link. Mail cannot be delivered until Amazon marks the address verified.</p>
    <p class="result" id="mail-verify-note"></p>
    <div class="popup-actions">
      <button type="button" id="mail-verify-close" class="quiet">Close</button>
      <button type="button" id="mail-verify-send" class="quiet">Send verification email</button>
      <button type="button" id="mail-verify-check">Check verification</button>
    </div>
  </div>
</div>
<div id="facts-overlay" hidden>
  <div class="popup" role="dialog" aria-modal="true" aria-labelledby="facts-title">
    <h2 id="facts-title">This check</h2>
    <dl class="facts">
      <div><dt>Account</dt><dd>{html.escape(ACCOUNT) or "not set"}</dd></div>
      <div><dt>Region</dt><dd>{html.escape(REGION) or "not set"}</dd></div>
      <div><dt>Inventory</dt><dd>{scanned}</dd></div>
      <div><dt>Catalog</dt><dd>{catalog_state}</dd></div>
    </dl>
    <div class="popup-actions"><button type="button" id="facts-close" class="quiet">Close</button></div>
  </div>
</div>
{_verify_overlay()}
"""
    return _page("SRE health checker", body)


def render_jira(demo: Demo) -> str:
    issue_views, jira_error = demo.issues_for_page()
    if not issue_views and not jira_error:
        _clear_stale_directory_failure(demo)
    banner_class, banner = _banner(demo)
    shown = []
    for view in issue_views:
        linked = _linked_issue(view, demo.rows)
        linked["proposed"] = resolved_proposal(linked, str(linked.get("proposed") or ""))
        linked["note"] = demo.ticket_notes.get(linked.get("key") or "", "")
        shown.append(linked)
    cards = _issue_cards(shown, jira_error)
    body = f"""
<header class="page-head">
  <div class="brand">
    <h1>Jira</h1>
    <p class="{banner_class}" id="status">{html.escape(banner)}</p>
  </div>
  <div class="top-actions"><a class="btn quiet" href="/">SRE health checker</a></div>
</header>
<main>
  <p class="meta">Open issues in the configured project. Edit the proposed correction, then confirm before anything runs.</p>
  {cards}
</main>
{_verify_overlay()}
"""
    return _page("Jira", body)


class DemoServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_handler(demo: Demo):
    mutators = {
        "break": demo.break_directory,
        "restore": demo.restore_directory,
    }
    refused = {"add", "remove", "topic", "delete-topic", "reset", "catalog"}
    legacy = {
        "/api/add": "add",
        "/api/remove": "remove",
        "/api/break": "break",
        "/api/restore": "restore",
        "/api/close": "close",
        "/api/topic": "topic",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                demo.open_health()
                page = render(demo).encode("utf-8")
                self._send(200, page, "text/html; charset=utf-8")
                return
            if path == "/jira":
                page = render_jira(demo).encode("utf-8")
                self._send(200, page, "text/html; charset=utf-8")
                return
            if path == "/api/status":
                payload = json.dumps(demo.status()).encode("utf-8")
                self._send(200, payload, "application/json")
                return
            if path == "/api/emails":
                payload = json.dumps({"addresses": load_recipients()}).encode("utf-8")
                self._send(200, payload, "application/json")
                return
            if path == "/favicon.ico":
                self._send(204, b"", "text/plain")
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            payload = self._read_json()
            if path == "/api/emails":
                if str(payload.get("verify") or "").strip():
                    saved = _mail_with_login(lambda: confirm_verification(str(payload.get("verify") or "")))
                elif str(payload.get("check") or "").strip():
                    saved = _mail_with_login(lambda: verification_status(str(payload.get("check") or "")))
                elif str(payload.get("delete") or "").strip():
                    saved = delete_recipient(str(payload.get("delete") or ""))
                elif str(payload.get("address") or "").strip():
                    saved = add_recipient(str(payload.get("address") or ""))
                else:
                    saved = save_recipients(str(payload.get("addresses") or ""))
                self._send(200 if saved["ok"] else 400, json.dumps(saved).encode("utf-8"), "application/json")
                return
            if path == "/api/catalog":
                body = json.dumps({"ok": False, "message": "This page does not change Confluence."}).encode("utf-8")
                self._send(400, body, "application/json")
                return
            action_name = str(payload.get("action") or legacy.get(path) or "")
            if path not in {"/api/plan", "/api/verify"} and path not in legacy:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            if action_name in refused or ALIASES.get(action_name) in refused:
                self._send(400, json.dumps({"ok": False, "applied": False, "error": "This page does not create or delete a service."}).encode("utf-8"), "application/json")
                return
            if action_name == "ticket":
                plan = demo.prepare_plan(action_name, payload)
            else:
                plan = plan_for(action_name, REGION)
            if plan is None or plan.get("error"):
                error = (plan or {}).get("error") or "Unknown action."
                self._send(400, json.dumps({"ok": False, "applied": False, "error": error}).encode("utf-8"), "application/json")
                return
            allowed = payload.get("verify") is True if action_name == "ticket" else allows_apply(action_name, payload)
            if path != "/api/verify" or not allowed:
                plan["ok"] = True
                plan["applied"] = False
                self._send(200, json.dumps(plan).encode("utf-8"), "application/json")
                return
            if action_name == "ticket" and plan.get("will_run") is False:
                message = plan.get("lead") or "This issue has no correction this page can run."
                body = json.dumps({"ok": True, "applied": False, "message": message, "mail": ""}).encode("utf-8")
                self._send(200, body, "application/json")
                return
            if action_name == "ticket":
                mutator = lambda: demo.take_issue(str(payload.get("issue") or ""), str(payload.get("proposed") or ""))
            else:
                mutator = mutators.get(plan["action"])
            if mutator is None or not demo.try_begin():
                self._send(409, json.dumps({"ok": False, "applied": False, "error": "Another action is still running."}).encode("utf-8"), "application/json")
                return
            try:
                mutator()
            except Exception as exc:
                demo.fail(exc)
                body = json.dumps({"ok": False, "applied": False, "error": demo.error, "mail": demo.mail_note}).encode("utf-8")
                self._send(500, body, "application/json")
                return
            body = json.dumps({"ok": True, "applied": True, "message": demo.message, "mail": demo.mail_note}).encode("utf-8")
            self._send(200, body, "application/json")

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}

        def _send(self, code: int, payload: bytes, content_type: str) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if payload:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

        def log_message(self, fmt: str, *args) -> None:
            print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    return Handler


def _catalog_example_parses() -> None:
    catalog = load_catalog(FakeConfluence(sns_storage(), version=1))
    if len(catalog.rules) != 1 or catalog.rules[0].resource_type != "sns:topic":
        raise RuntimeError("The SNS paste example does not parse as sns:topic.")
    if catalog.rules[0].role != "primary" or catalog.rules[0].comparison != "GreaterThanThreshold":
        raise RuntimeError("The SNS paste example does not keep the primary greater-than rule.")


def main() -> int:
    bind_settings()
    os.environ.pop("SRE_SERVICE_LIST_URI", None)
    _catalog_example_parses()
    demo = Demo()
    try:
        refresh_aws_login()
    except Exception as exc:
        demo.fail(exc)
        demo.message = "AWS credentials are not ready."
        with demo._lock:
            demo.phase = demo.message + " " + demo.error
            demo._busy = False
        print(demo.phase, flush=True)
    server = None
    last_bind_error = "port busy"
    for _ in range(30):
        try:
            server = DemoServer((HOST, PORT), make_handler(demo))
            break
        except OSError as exc:
            last_bind_error = public_text(exc)
            time.sleep(2)
    if server is None:
        print(f"Could not bind {HOST}:{PORT}. {last_bind_error}", flush=True)
        return 1
    print(f"Demo UI at http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
