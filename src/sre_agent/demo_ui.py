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
from sre_agent.demo_mail import CLEANUP_MAIL, SIMULATION_MAIL, deliver_mail, load_recipients, save_recipients
from sre_agent.demo_plans import (
    NOT_RUN,
    NOT_RUNNABLE,
    ALIASES,
    allows_apply,
    lab_target_ids,
    plan_for,
    ticket_decision,
    ticket_plan,
)
from sre_agent.integrations.confluence import FakeConfluence, load_catalog
from sre_agent.local_config import get, load_local_config

HOST = "127.0.0.1"
PORT = 8765
REGION = ""
ACCOUNT = ""
DIRECTORY = "enterprise-sites-directory"
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
        detail = _error_text(exc)
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

    def ensure_fresh(self) -> None:
        """Read the account before drawing the page. A previous scan is not reused."""
        while not self.try_begin():
            time.sleep(0.25)
        try:
            self.set_phase("Running inventory.", log=True)
            self._inventory(display=True)
            count = sum(1 for row in self.rows if row.get("presence") in {"present", ""})
            self.finish(f"Baseline inventory completed. {count} resources.")
            self._log_rows()
        except Exception as exc:
            self.fail(exc)

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

    def restore_directory(self, *, mail: bool = True, close: str = "always", issue_key: str = "") -> None:
        if issue_key:
            self.directory_issue_key = issue_key
        self.set_phase("Pointing enterprise-sites-directory at TABLE_NAME=enterprise-sites.", log=True)
        self._set_directory_table(LIVE_TABLE)
        self.set_phase("Configuration update is active. Invoking the sites URL.", log=True)
        codes = _invoke_sites()
        mark = datetime.now(timezone.utc)
        self.set_phase(
            "Sites URL responses: " + _codes(codes) + ". Waiting for a period with successful invocations and no errors.",
            log=True,
        )

        def ready() -> bool:
            errors = _lambda_metric("Errors", mark - timedelta(minutes=3))
            invocations = _lambda_metric("Invocations", mark - timedelta(minutes=3))
            recent = [
                point
                for point in invocations
                if _aware(point["Timestamp"]) >= mark - timedelta(seconds=90) and float(point.get("Sum") or 0) > 0
            ]
            if not recent:
                return False
            newest = max(recent, key=lambda point: _aware(point["Timestamp"]))
            ok_at = _aware(newest["Timestamp"])
            matched = [point for point in errors if _aware(point["Timestamp"]) >= ok_at - timedelta(seconds=5)]
            if not matched:
                return False
            latest = max(matched, key=lambda point: _aware(point["Timestamp"]))
            return float(latest.get("Sum") or 0) == 0

        seen = self._wait(
            minimum=60,
            maximum=90,
            label="Waiting for a clean Lambda period after the restore.",
            ready=ready,
        )
        self.set_phase("Running inventory.", log=True)
        self._inventory()
        health, code = self._directory_health()
        clean = "A clean CloudWatch period was visible." if seen else "A clean CloudWatch period was not visible within 90 seconds."
        if close == "healthy":
            resource_id = ""
            for row in self.rows:
                if row["name"] == DIRECTORY and row["resource_type"] == "lambda:function":
                    resource_id = row.get("resource_id", "")
                    break
            closed = self._close_if_healthy(issue_key or self.directory_issue_key, resource_id, DIRECTORY, "lambda:function")
        else:
            closed = self._close_verified_ticket()
        self.directory_note = (
            f"TABLE_NAME is enterprise-sites. Responses {_codes(codes)}. {clean} "
            f"Directory health is {health or 'not in this inventory'}. Problem code {code or 'none'}. "
            f"{closed}"
        )
        if mail:
            self.mail_note = deliver_mail("SRE agent: enterprise-sites-directory restored", self.directory_note)
        self.finish(self.directory_note)

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
        self.agent = agent
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
        return f"{key} was closed {phrase}."

    def _close_if_healthy(self, key: str, resource_id: str = "", name: str = "", resource_type: str = "") -> str:
        health, code = self._health_for(resource_id, name, resource_type)
        if health == "healthy":
            return self._close_issue(key)
        return _stay_open(health, code)

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
        if proposed.strip():
            view = {**view, "proposed": proposed.strip()}
        decision = _decision_for(view)
        if not decision["run"]:
            note = "The edited action was not run. The ticket stays open."
            self.ticket_notes[key] = note
            self.mail_note = "Mail was not sent."
            self.finish(note)
            return
        if decision["kind"] == "restore-directory":
            self.restore_directory(mail=False, close="healthy", issue_key=key)
            self.ticket_notes[key] = self.directory_note
            self.mail_note = "Mail was not sent."
            return
        if decision["kind"] == "redrive":
            note = self._run_lab_redrive(decision["target"], view.get("resource_name", ""))
            self.set_phase("Running inventory.", log=True)
            self._inventory()
            closed = self._close_if_healthy(key, decision["target"], view.get("resource_name", ""), view.get("resource_type", ""))
            text = f"{note} {closed}"
            self.ticket_notes[key] = text
            self.mail_note = "Mail was not sent."
            self.finish(text)
            return
        self.ticket_notes[key] = NOT_RUNNABLE
        self.mail_note = "Mail was not sent."
        self.finish(NOT_RUNNABLE)

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
        if record is None or not explicit_lab_target(record.resource_id, record.name, targets):
            return NOT_RUN
        self.set_phase("Checking the lab queue before sqs.redrive_dlq.", log=True)
        tools = agent.tools
        tools.inspect(record.resource_id, "demo-ticket")
        snapshot = tools.snapshot()
        lines: list[str] = []
        for read in snapshot.logs.values():
            lines.extend(getattr(read, "lines", []) or [])
        eligible, _rejected, notes = order_outcomes(list(getattr(snapshot, "dlq_messages", []) or []), lines)
        if not eligible:
            detail = notes[0] if notes else "No verified transient orders to replay."
            return f"sqs.redrive_dlq did not move messages. {public_text(detail)}"
        policy = (record.config or {}).get("redrive_policy") or {}
        dlq = str(policy.get("deadLetterTargetArn") or "")
        if not dlq:
            return "sqs.redrive_dlq did not move messages. The queue has no dead-letter target."
        reader = _reader_for(agent.scope, record.account_id)
        if reader is None or not hasattr(reader, "replay_orders"):
            return NOT_RUN
        result = reader.replay_orders(record.account_id, record.region, record.resource_id, dlq, eligible)
        return f"sqs.redrive_dlq ran on the lab target. Moved {result.get('moved', 0)}."

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
        if proposed.strip():
            view = {**view, "proposed": proposed.strip()}
        return ticket_plan(key, _decision_for(view), REGION, proposed=str(view.get("proposed") or ""))

    def populate_workload(self, fields: dict) -> None:
        from sre_agent.integrations.confluence import (
            storage_has_service,
            validate_workload_fields,
            workload_section,
        )
        from sre_agent.worker import _confluence

        error = validate_workload_fields(fields)
        if error:
            raise RuntimeError(error)
        client = _confluence()
        if client is None or not hasattr(client, "fetch_document"):
            raise RuntimeError("Confluence is not configured.")
        self.set_phase("Reading the Confluence page.", log=True)
        version, title, storage, space_id = client.fetch_document()
        service = str(fields.get("service") or "").strip()
        resource_type = str(fields.get("resource_type") or "").strip()
        heading = f"Service: {service} ({resource_type})"
        if storage_has_service(storage, service, resource_type):
            note = f"{heading} is already on the Confluence page."
            self.catalog_note = note
            self.mail_note = ""
            self.finish(note)
            return
        section = workload_section(fields, storage)
        self.set_phase(f"Adding {heading}.", log=True)
        client.write_storage(title, storage + section, version, space_id)
        note = f"Added {heading} to the Confluence page."
        self.catalog_note = note
        self.mail_note = ""
        self.finish(note)

    def _issue_view(self, key: str) -> dict | None:
        views, _error = self.issues_for_page()
        for view in views:
            if view.get("key") == key:
                return view
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
                links[diagnosis.jira_issue_key] = {
                    "resource_id": diagnosis.resource_id,
                    "name": record.name,
                    "resource_type": record.resource_type,
                    "target": action.target_resource_id if action is not None else diagnosis.resource_id,
                }
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
            if scan.status in {"error", "denied", "partial"}:
                notes.append(f"{scan.collector} ({scan.region}) {scan.status}: {public_text(scan.message)}")
        self.report = report
        self.rows = rows
        self.scan_notes = notes
        self.scanned_at = max(scanned) if scanned else ""
        self.catalog_status = agent.catalog_status
        self.catalog_types = sorted({rule.resource_type for rule in agent.catalog_rules})

    def _set_directory_table(self, table_name: str) -> None:
        if table_name not in {LIVE_TABLE, "missing-table"}:
            raise RuntimeError("Refusing to set an unexpected table name.")
        refresh_aws_login()
        import boto3

        client = boto3.Session(region_name=REGION).client("lambda", region_name=REGION)
        waiter = client.get_waiter("function_updated")
        config = {"Delay": 2, "MaxAttempts": 40}
        waiter.wait(FunctionName=DIRECTORY, WaiterConfig=config)
        try:
            client.update_function_configuration(
                FunctionName=DIRECTORY,
                Environment={"Variables": {"TABLE_NAME": table_name}},
            )
        except client.exceptions.ResourceConflictException:
            waiter.wait(FunctionName=DIRECTORY, WaiterConfig=config)
            client.update_function_configuration(
                FunctionName=DIRECTORY,
                Environment={"Variables": {"TABLE_NAME": table_name}},
            )
        self.set_phase(f"Waiting until the configuration update for TABLE_NAME={table_name} is active.", log=True)
        waiter.wait(FunctionName=DIRECTORY, WaiterConfig=config)

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


def _stay_open(health: str, code: str) -> str:
    state = shown_health(health) if health else "degraded"
    text = f"Health is {state}."
    if code and code != "healthy":
        text += f" Problem code {code}."
    return text + " The ticket stays open because health is not healthy."


def _decision_for(view: dict) -> dict:
    return ticket_decision(
        str(view.get("proposed") or ""),
        summary=str(view.get("summary") or ""),
        resource_name=str(view.get("resource_name") or ""),
        resource_id=str(view.get("resource_id") or ""),
        resource_type=str(view.get("resource_type") or ""),
        lab_targets=lab_target_ids(),
    )


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
    if code and row.get("problem"):
        detail = code + " " + row["problem"]
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
        cards.append(
            "<article class='card'>"
            f"<header>{key_html}<span class='status-pill'>{html.escape(issue.get('status') or 'Open')}</span></header>"
            f"<h3>Issue</h3><p class='situation'>{html.escape(issue.get('situation') or '')}</p>"
            f"<h3>Where seen</h3><p class='where'>{html.escape(issue.get('where_seen') or issue.get('task') or '')}</p>"
            f"<label>Proposed solution<textarea class='proposed'>{proposed}</textarea></label>"
            f"{note_html}"
            f"<button type='button' class='ticket-run' data-issue='{key}'>Take this action</button>"
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


def _lambda_metric(name: str, start: datetime) -> list[dict]:
    refresh_aws_login()
    import boto3

    client = boto3.Session(region_name=REGION).client("cloudwatch", region_name=REGION)
    response = client.get_metric_statistics(
        Namespace="AWS/Lambda",
        MetricName=name,
        Dimensions=[{"Name": "FunctionName", "Value": DIRECTORY}],
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


def _export_aws_login() -> None:
    global _session_expires_at
    command = ["aws", "configure", "export-credentials", "--format", "process"]
    profile = get("AWS_PROFILE")
    if profile:
        command = ["aws", "--profile", profile, "configure", "export-credentials", "--format", "process"]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("AWS CLI login credentials are not available. Run aws login, then start this server again.")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWS CLI login credentials could not be read.") from exc
    access = str(payload.get("AccessKeyId") or "")
    secret = str(payload.get("SecretAccessKey") or "")
    if not access or not secret:
        raise RuntimeError("AWS CLI login credentials were incomplete.")
    os.environ["AWS_ACCESS_KEY_ID"] = access
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret
    token = str(payload.get("SessionToken") or "")
    if token:
        os.environ["AWS_SESSION_TOKEN"] = token
    else:
        os.environ.pop("AWS_SESSION_TOKEN", None)
    os.environ["AWS_DEFAULT_REGION"] = REGION
    os.environ["AWS_REGION"] = REGION
    _session_expires_at = _parse_expiration(str(payload.get("Expiration") or ""))


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
#facts-overlay, #verify-overlay, #test-overlay, #conditions-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  z-index: 5;
}
#facts-overlay[hidden], #verify-overlay[hidden], #test-overlay[hidden], #conditions-overlay[hidden] { display: none; }
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
const testTool = document.getElementById("test-tool");
if (testTool) {
  testTool.addEventListener("click", () => {
    document.getElementById("test-overlay").hidden = false;
  });
}
const testClose = document.getElementById("test-close");
if (testClose) {
  testClose.addEventListener("click", () => {
    document.getElementById("test-overlay").hidden = true;
  });
}
document.querySelectorAll("button[data-plan]").forEach(button => {
  button.addEventListener("click", () => {
    const testOverlay = document.getElementById("test-overlay");
    if (testOverlay) testOverlay.hidden = true;
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
const saveEmails = document.getElementById("save-emails");
if (saveEmails) {
  saveEmails.addEventListener("click", async () => {
    const response = await fetch("/api/emails", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({addresses: document.getElementById("mail-addresses").value}),
    });
    const body = await response.json();
    document.getElementById("email-status").textContent = body.message || "";
    if (body.ok && Array.isArray(body.addresses)) {
      document.getElementById("mail-addresses").value = body.addresses.join("\\n");
    }
  });
}
const factsOpen = document.getElementById("facts-open");
const facts = document.getElementById("facts-overlay");
if (factsOpen && facts) {
  factsOpen.addEventListener("click", () => { facts.hidden = false; });
  document.getElementById("facts-close").addEventListener("click", () => { facts.hidden = true; });
}
const workload = document.getElementById("workload-form");
const populate = document.getElementById("populate");
function conditionValues() {
  return Array.from(document.querySelectorAll("#condition-rows .condition-row")).map(row => ({
    condition: (row.querySelector("[data-field=condition]") || {}).value || "",
    threshold: (row.querySelector("[data-field=threshold]") || {}).value || "",
    kind: (row.querySelector("[data-field=kind]") || {}).value || "",
    namespace: (row.querySelector("[data-field=namespace]") || {}).value || "",
  })).map(row => ({
    condition: String(row.condition).trim(),
    threshold: String(row.threshold).trim(),
    kind: String(row.kind).trim(),
    namespace: String(row.namespace).trim(),
  }));
}
function conditionReady(row) {
  return [">", ">=", "<"].includes(row.condition)
    && row.threshold
    && ["metric", "log"].includes(row.kind)
    && row.namespace;
}
function catalogReady() {
  if (!workload) return false;
  const data = Object.fromEntries(new FormData(workload));
  const required = ["service", "resource_type", "measure", "dimension", "stat", "window", "role"];
  const filled = required.every(name => String(data[name] || "").trim());
  const windowOk = /^[1-9][0-9]*$/.test(String(data.window || "").trim());
  const dimensionOk = String(data.dimension || "").includes("=");
  const shared = filled && windowOk && dimensionOk
    && ["primary", "supporting"].includes(data.role)
    && ["Sum", "Average", "Maximum", "Minimum", "Count"].includes(data.stat);
  const rows = conditionValues().filter(row => row.condition || row.threshold || row.kind || row.namespace);
  return shared && rows.length > 0 && rows.every(conditionReady);
}
if (workload && populate) {
  const refreshPopulate = () => { populate.disabled = !catalogReady(); };
  workload.addEventListener("input", refreshPopulate);
  workload.addEventListener("change", refreshPopulate);
  const conditionRows = document.getElementById("condition-rows");
  if (conditionRows) {
    conditionRows.addEventListener("input", refreshPopulate);
    conditionRows.addEventListener("change", refreshPopulate);
    conditionRows.addEventListener("click", event => {
      const button = event.target.closest(".condition-add");
      if (!button) return;
      const source = conditionRows.querySelector(".condition-row");
      if (!source) return;
      const copy = source.cloneNode(true);
      copy.querySelectorAll("input").forEach(input => { input.value = ""; });
      copy.querySelectorAll("select").forEach(select => { select.selectedIndex = 0; });
      conditionRows.appendChild(copy);
      refreshPopulate();
    });
  }
  const conditionsOpen = document.getElementById("conditions-open");
  const conditionsOverlay = document.getElementById("conditions-overlay");
  if (conditionsOpen && conditionsOverlay) {
    conditionsOpen.addEventListener("click", () => { conditionsOverlay.hidden = false; });
    document.getElementById("conditions-close").addEventListener("click", () => { conditionsOverlay.hidden = true; });
  }
  populate.addEventListener("click", async () => {
    if (!catalogReady()) return;
    populate.disabled = true;
    const data = Object.fromEntries(new FormData(workload));
    data.conditions = conditionValues().filter(row => row.condition || row.threshold || row.kind || row.namespace);
    const response = await fetch("/api/catalog", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data),
    });
    const body = await response.json().catch(() => ({}));
    const result = document.getElementById("catalog-result");
    if (result) result.textContent = body.message || body.error || "";
    refreshPopulate();
  });
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


def render(demo: Demo) -> str:
    banner_class, banner = _banner(demo)
    directory = next(
        (row for row in demo.rows if row["name"] == DIRECTORY and row["resource_type"] == "lambda:function"),
        None,
    )
    if directory is None:
        directory_health = "not in this inventory"
        directory_code = ""
    else:
        directory_health = shown_health(directory["health"])
        directory_code = directory["problem_code"]
    issue = demo.directory_issue_key
    issue_html = html.escape(issue) if issue else "None yet. Break the directory records it."
    issue_url = _browse(issue)
    if issue and issue_url:
        issue_html = f'<a href="{html.escape(issue_url)}">{html.escape(issue)}</a>'
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
    notes = ""
    if demo.scan_notes:
        items = "".join(f"<li>{html.escape(note)}</li>" for note in demo.scan_notes)
        notes = f"<div class='notes'><strong>Scan problems</strong><ul>{items}</ul></div>"
    directory_result = html.escape(demo.directory_note) if demo.directory_note else "The issue key appears here after you press Break the directory."
    scanned = html.escape(demo.scanned_at) if demo.scanned_at else "not completed"
    saved_emails = html.escape("\n".join(load_recipients()))
    catalog_state = html.escape(demo.catalog_status)
    page_link = _confluence_url()
    prompt = "Nothing is written until you press Populate. Please check the changes at: "
    if demo.catalog_note:
        catalog_result = html.escape(demo.catalog_note)
    elif page_link:
        catalog_result = (
            html.escape(prompt)
            + f'<a href="{html.escape(page_link)}">{html.escape(page_link)}</a>'
        )
    else:
        catalog_result = html.escape(prompt)
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
    <p>{catalog_html}</p>
    <form id="workload-form" class="form-grid">
      <label>Service name<span class="hint">for example EKS</span><input name="service" autocomplete="off"></label>
      <label>Resource type<span class="hint">for example eks:cluster</span><input name="resource_type" autocomplete="off"></label>
      <label>Symptom id<span class="hint">optional; generated from the service name if blank</span><input name="symptom_id" autocomplete="off"></label>
      <label>What to measure<span class="hint">metric name or log query</span><input name="measure" autocomplete="off"></label>
      <div class="measure-line">
        <label>Dimension<span class="hint">Name=Value, placeholders in braces allowed</span><input name="dimension" autocomplete="off"></label>
        <label>Stat
          <select name="stat">
            <option value=""></option>
            <option>Sum</option>
            <option>Average</option>
            <option>Maximum</option>
            <option>Minimum</option>
            <option>Count</option>
          </select>
        </label>
        <label>Window in minutes<input name="window" inputmode="numeric" autocomplete="off"></label>
        <label>Role
          <select name="role">
            <option value=""></option>
            <option value="primary">primary</option>
            <option value="supporting">supporting</option>
          </select>
        </label>
      </div>
    </form>
    <p><button type="button" id="conditions-open">Add</button></p>
    <p><button type="button" id="populate" disabled>Populate</button></p>
    <p id="catalog-result" class="result">{catalog_result}</p>
  </section>
</main>
<footer>
  <section>
    <h2>Let me test how this works</h2>
    <p><button type="button" id="test-tool">Let me test how this works</button></p>
    <p class="meta">Break and restore stay in that popup. Nothing changes until Verify and apply.</p>
  </section>
  <section>
    <h2>Mail</h2>
    <label for="mail-addresses">Send results to these addresses</label>
    <textarea id="mail-addresses" name="mail-addresses">{saved_emails}</textarea>
    <p><button type="button" id="save-emails">Save addresses</button></p>
    <p class="meta" id="ses-note">Amazon SES sends from the verified From address in the local config. In the sandbox, each recipient must be verified too.</p>
    <p id="email-status" class="meta"></p>
  </section>
</footer>
<div id="conditions-overlay" hidden>
  <div class="popup" role="dialog" aria-modal="true" aria-labelledby="conditions-title">
    <h2 id="conditions-title">Conditions</h2>
    <div id="condition-rows">
      <div class="condition-row">
        <label>Condition
          <select data-field="condition">
            <option value=""></option>
            <option value=">">&gt;</option>
            <option value=">=">&gt;=</option>
            <option value="<">&lt;</option>
          </select>
        </label>
        <label>Threshold<input data-field="threshold" autocomplete="off"></label>
        <label>Type
          <select data-field="kind">
            <option value=""></option>
            <option value="metric">metric</option>
            <option value="log">log</option>
          </select>
        </label>
        <label>Namespace or log group<input data-field="namespace" autocomplete="off"></label>
        <button type="button" class="condition-add">Add</button>
      </div>
    </div>
    <div class="popup-actions"><button type="button" id="conditions-close" class="quiet">Close</button></div>
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
<div id="test-overlay" hidden>
  <div class="popup" role="dialog" aria-modal="true" aria-labelledby="test-title">
    <h2 id="test-title">Let me test how this works</h2>
    <p>Directory health now: <strong>{html.escape(directory_health)}</strong>. Problem code: {html.escape(directory_code) or "none"}.</p>
    <p>Directory issue in this inventory: {issue_html}</p>
    <p class="meta">These buttons are not on the main page. Each one opens the action list, and nothing changes until you press Verify and apply.</p>
    <div class="popup-actions" style="justify-content: flex-start; flex-wrap: wrap; margin: 16px 0;">
      <button type="button" data-plan="break">Break the directory</button>
      <button type="button" class="quiet" data-plan="restore">Restore the directory</button>
    </div>
    <p class="meta">Break points enterprise-sites-directory at a missing table, calls the sites URL, and runs inventory. Restore sets TABLE_NAME to enterprise-sites.</p>
    <div class="result" id="directory-note">{directory_result}</div>
    <div class="popup-actions">
      <button type="button" id="test-close" class="quiet">Close</button>
    </div>
  </div>
</div>
{_verify_overlay()}
"""
    return _page("SRE health checker", body)


def render_jira(demo: Demo) -> str:
    banner_class, banner = _banner(demo)
    issue_views, jira_error = demo.issues_for_page()
    for view in issue_views:
        view["note"] = demo.ticket_notes.get(view.get("key") or "", "")
    cards = _issue_cards(issue_views, jira_error)
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
                demo.ensure_fresh()
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
                saved = save_recipients(str(payload.get("addresses") or ""))
                self._send(200 if saved["ok"] else 400, json.dumps(saved).encode("utf-8"), "application/json")
                return
            if path == "/api/catalog":
                if not demo.try_begin():
                    self._send(409, json.dumps({"ok": False, "message": "Another action is still running."}).encode("utf-8"), "application/json")
                    return
                try:
                    demo.populate_workload(payload)
                except Exception as exc:
                    demo.fail(exc)
                    body = json.dumps({"ok": False, "message": demo.error}).encode("utf-8")
                    self._send(400, body, "application/json")
                    return
                body = json.dumps({"ok": True, "message": demo.message}).encode("utf-8")
                self._send(200, body, "application/json")
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
        print("Run aws login, then start this server again.", flush=True)
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
