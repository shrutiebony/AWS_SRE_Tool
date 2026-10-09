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

from sre_agent.cli import _load_local_env, run_live_cycle
from sre_agent.integrations.confluence import FakeConfluence, load_catalog

HOST = "127.0.0.1"
PORT = 8765
REGION = "us-east-2"
ACCOUNT = "414279120882"
DIRECTORY = "enterprise-sites-directory"
LIVE_TABLE = "enterprise-sites"
EXTRA_TABLE = "demo-extra-sites"
TOPIC_NAME = "demo-alerts"
SITES_URL = "https://af38fctvq2.execute-api.us-east-2.amazonaws.com/prod/sites"
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


def sns_plain() -> str:
    lines = [SNS_HEADING, "\t".join(SNS_COLUMNS), "\t".join(SNS_ROW)]
    return "\n".join(lines)


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
            return True

    def finish(self, message: str) -> None:
        safe = public_text(message)
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
        self.finish(self.directory_note)

    def restore_directory(self) -> None:
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
        key = self.directory_issue_key or "the KAN task"
        clean = "A clean CloudWatch period was visible." if seen else "A clean CloudWatch period was not visible within 90 seconds."
        self.directory_note = (
            f"TABLE_NAME is enterprise-sites. Responses {_codes(codes)}. {clean} "
            f"Directory health is {health or 'not in this inventory'}. Problem code {code or 'none'}. "
            f"{key} stays open because Done happens only after a verified SQS redrive."
        )
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
        if completed.returncode == 0:
            self.finish("Lab finished with exit code 0. Walk unhealthy, task, redrive, verified, Done from the result.")
        else:
            self.finish(f"Lab finished with exit code {completed.returncode}.")

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
        self.finish(self.topic_note)

    def _inventory(self) -> None:
        refresh_aws_login()
        os.environ.pop("SRE_SERVICE_LIST_URI", None)
        report, agent = run_live_cycle(ACCOUNT, REGION)
        self._apply(report, agent)

    def _apply(self, report, agent) -> None:
        by_id = {record.resource_id: record for record in report.resources}
        codes: dict[str, list[str]] = {}
        problems: dict[str, str] = {}
        for diagnosis in report.diagnoses:
            if diagnosis.problem_code:
                codes.setdefault(diagnosis.resource_id, []).append(diagnosis.problem_code)
            if diagnosis.observed_problem:
                problems.setdefault(diagnosis.resource_id, diagnosis.observed_problem)
            record = by_id.get(diagnosis.resource_id)
            if record is not None and record.name == DIRECTORY and diagnosis.jira_issue_key:
                self.directory_issue_key = diagnosis.jira_issue_key
        rows = []
        for record in sorted(report.resources, key=lambda item: (item.resource_type, item.name.lower())):
            rows.append(
                {
                    "name": record.name,
                    "resource_type": record.resource_type,
                    "health": record.health,
                    "problem_code": ", ".join(dict.fromkeys(codes.get(record.resource_id, []))),
                    "problem": problems.get(record.resource_id, ""),
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
                "The heading Service: SNS (sns:topic) is not on Confluence page 360481 yet, "
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
    base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    if not base or not key:
        return ""
    return base + "/browse/" + key


def _confluence_url() -> str:
    base = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
    page = os.environ.get("CONFLUENCE_PAGE_ID", "360481")
    if not base or not page:
        return ""
    return f"{base}/wiki/pages/viewpage.action?pageId={page}"


def _error_text(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = str(error.get("Code") or "")
        message = str(error.get("Message") or "")
        if code or message:
            return public_text(f"{code}: {message}".strip(": "))
    return public_text(exc)


def refresh_aws_login() -> None:
    """Copy the AWS CLI login session into this process. Values are not logged."""
    completed = subprocess.run(
        ["aws", "configure", "export-credentials", "--format", "process"],
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


def render(demo: Demo) -> str:
    banner_class = "status bad" if demo.error else "status"
    banner = demo.error if demo.error and not demo.message else (demo.message or demo.phase)
    if demo.error and demo.message:
        banner = demo.message + " " + demo.error
    directory = next(
        (row for row in demo.rows if row["name"] == DIRECTORY and row["resource_type"] == "lambda:function"),
        None,
    )
    if directory is None:
        directory_health = "not in this inventory"
        directory_code = ""
    else:
        directory_health = directory["health"]
        directory_code = directory["problem_code"]
    issue = demo.directory_issue_key
    issue_html = html.escape(issue) if issue else "None yet. Break the directory records it."
    issue_url = _jira_url(issue)
    if issue and issue_url:
        issue_html = f'<a href="{html.escape(issue_url)}">{html.escape(issue)}</a>'
    catalog_link = _confluence_url()
    catalog_html = (
        f'<a href="{html.escape(catalog_link)}">Confluence page 360481</a>'
        if catalog_link
        else "Confluence page 360481"
    )
    rows_html = []
    for row in demo.rows:
        focus = row["name"] in {EXTRA_TABLE, TOPIC_NAME, DIRECTORY, LIVE_TABLE}
        health = row["health"] or "unknown"
        code = row["problem_code"]
        presence = "" if row["presence"] == "present" else " (" + row["presence"] + ")"
        detail = code
        if code and row.get("problem"):
            detail = code + " " + row["problem"]
        rows_html.append(
            "<tr class='{focus}'>".format(focus="focus" if focus else "")
            + f"<td>{html.escape(row['name'] + presence)}</td>"
            + f"<td>{html.escape(row['resource_type'])}</td>"
            + f"<td><span class='health {html.escape(health)}'>{html.escape(health)}</span></td>"
            + f"<td>{html.escape(detail)}</td>"
            + f"<td>{html.escape(row['discovery_source'])}</td>"
            + "</tr>"
        )
    if not rows_html:
        body = "<tr><td colspan='5'>No resources in this inventory.</td></tr>"
    else:
        body = "\n".join(rows_html)
    notes = ""
    if demo.scan_notes:
        items = "".join(f"<li>{html.escape(note)}</li>" for note in demo.scan_notes)
        notes = f"<div class='notes'><strong>Scan problems</strong><ul>{items}</ul></div>"
    storage = sns_storage()
    plain = sns_plain()
    header_cells = "".join(f"<th>{html.escape(name)}</th>" for name in SNS_COLUMNS)
    row_cells = "".join(f"<td>{html.escape(value)}</td>" for value in SNS_ROW)
    lab_block = html.escape(demo.lab_output) if demo.lab_output else "The lab result appears here after you press Show the close."
    lab_meta = ""
    if demo.lab_code is not None:
        lab_meta = f"<p class='meta'>Exit code {demo.lab_code}.</p>"
    topic = html.escape(demo.topic_note) if demo.topic_note else "The scan result appears here after you press Create the topic and scan."
    change = html.escape(demo.change_note) if demo.change_note else "The registry below updates after each button."
    directory_result = html.escape(demo.directory_note) if demo.directory_note else "The issue key appears here after you press Break the directory."
    scanned = html.escape(demo.scanned_at) if demo.scanned_at else "not completed"
    catalog_state = html.escape(demo.catalog_status)
    types = html.escape(", ".join(demo.catalog_types)) if demo.catalog_types else "none"
    restart = html.escape(
        'cd C:\\Users\\sgoya\\OneDrive\\Documents\\Projects\\SRE_TOOL ; $env:PYTHONPATH = "src" ; python -m sre_agent.demo_ui'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SRE health checker</title>
<style>
  :root {{
    color-scheme: light;
    --ink: #1c2833;
    --muted: #5c6b7a;
    --line: #d5dbe3;
    --paper: #f3f5f7;
    --panel: #ffffff;
    --navy: #1f4e79;
    --sticky: #e7eef5;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font: 16px/1.45 "Segoe UI", sans-serif;
  }}
  header, main {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; }}
  header {{ padding: 28px 0 8px; }}
  h1 {{ font-size: 28px; font-weight: 650; margin: 0 0 6px; letter-spacing: -0.02em; }}
  h2 {{ font-size: 20px; margin: 0 0 8px; }}
  h3 {{ font-size: 16px; margin: 18px 0 6px; }}
  p {{ margin: 8px 0; }}
  a {{ color: var(--navy); }}
  .meta, .say {{ color: var(--muted); }}
  .say {{
    margin: 8px 0 12px;
    padding-left: 12px;
    border-left: 3px solid #c5d0dc;
  }}
  .status {{
    position: sticky;
    top: 0;
    z-index: 2;
    background: var(--sticky);
    border-bottom: 1px solid var(--line);
    padding: 12px 16px;
  }}
  .status.bad {{ background: #f8ecec; }}
  .wrap {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    font-size: 14px;
  }}
  th, td {{
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
  }}
  th {{ color: var(--muted); font-weight: 650; background: #f7f9fb; }}
  tr.focus {{ background: #f4f8fc; }}
  .health {{
    display: inline-block;
    min-width: 88px;
    padding: 2px 8px;
    border-radius: 999px;
    background: #e6ebf0;
    text-align: center;
  }}
  .health.healthy {{ background: #e5f4ea; color: #1e7a46; }}
  .health.degraded {{ background: #f8f1df; color: #8a5a00; }}
  .health.unhealthy {{ background: #f8e6e6; color: #9b2c2c; }}
  .health.unknown {{ background: #e6ebf0; color: #5c6b7a; }}
  section {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 18px 18px 16px;
    margin: 16px 0;
  }}
  .inventory {{ margin-top: 18px; }}
  button {{
    appearance: none;
    border: 0;
    border-radius: 4px;
    padding: 10px 16px;
    font: inherit;
    cursor: pointer;
    margin: 4px 8px 4px 0;
  }}
  button.action {{ background: var(--navy); color: white; }}
  button.quiet {{ background: #4d5d6c; color: white; }}
  button.copy {{ background: #e6ebf0; color: var(--ink); }}
  button:disabled {{ opacity: 0.55; cursor: wait; }}
  .step {{ padding-top: 4px; border-top: 1px solid var(--line); margin-top: 14px; }}
  .step:first-of-type {{ border-top: 0; margin-top: 0; padding-top: 0; }}
  .result {{
    background: #f7f9fb;
    border-radius: 6px;
    padding: 10px 12px;
    margin-top: 8px;
  }}
  pre {{
    white-space: pre-wrap;
    word-break: break-word;
    font: 13px/1.4 ui-monospace, Consolas, monospace;
    margin: 8px 0 0;
  }}
  .notes {{
    background: #f8f1df;
    border-radius: 6px;
    padding: 10px 12px;
    margin: 0 0 12px;
  }}
  .paste table {{ font-size: 13px; }}
  footer {{ color: var(--muted); font-size: 13px; padding: 8px 0 28px; }}
  code {{ font-family: ui-monospace, Consolas, monospace; font-size: 0.92em; }}
</style>
</head>
<body>
<div class="{banner_class}"><div class="wrap" id="status">{html.escape(banner)}</div></div>
<header>
  <h1>SRE health checker</h1>
  <p class="meta">Account {html.escape(ACCOUNT)} · Region {html.escape(REGION)} · Inventory {scanned} · Catalog {catalog_state}</p>
  <p><a href="{html.escape(SITES_URL)}">{html.escape(SITES_URL)}</a></p>
</header>
<main>
  <section class="inventory">
    <h2>Inventory</h2>
    <p class="meta">The page opens on a completed baseline. Each button runs a new inventory and refreshes this table. {len(demo.rows)} resources. Extra catalog types: {types}. SRE_SERVICE_LIST_URI is unset.</p>
    {notes}
    <table>
      <thead>
        <tr><th>Name</th><th>Type</th><th>Health</th><th>Problem code</th><th>Source</th></tr>
      </thead>
      <tbody>
        {body}
      </tbody>
    </table>
  </section>

  <section id="panel-add">
    <h2>1. Addition and deletion</h2>
    <p class="say">Say: The checker inventories what is already in the account. I will add a DynamoDB table it has not seen, then remove it.</p>
    <div class="step">
      <h3>Add</h3>
      <button type="button" class="action" data-action="/api/add">Add a resource</button>
      <p class="meta">Creates DynamoDB table demo-extra-sites in us-east-2, partition key site_id (String), on-demand capacity, then runs inventory.</p>
    </div>
    <div class="step">
      <h3>Remove</h3>
      <button type="button" class="action quiet" data-action="/api/remove">Remove it</button>
      <p class="meta">Deletes only demo-extra-sites, then runs inventory.</p>
    </div>
    <div class="result" id="change-note">{change}</div>
  </section>

  <section id="panel-jira">
    <h2>2. Failure opens a Jira task</h2>
    <p class="say">Say: I will point the site directory at a table that does not exist. Inventory opens a KAN task. Restoring the function makes it healthy again. The task stays open because Done happens only after a verified SQS redrive.</p>
    <p>Directory health now: <strong>{html.escape(directory_health)}</strong>. Problem code: {html.escape(directory_code) or "none"}.</p>
    <p>Directory issue in this inventory: {issue_html}</p>
    <p><strong>The task stays open because Done happens only after a verified SQS redrive.</strong></p>
    <div class="step">
      <h3>Break</h3>
      <button type="button" class="action" data-action="/api/break">Break the directory</button>
      <p class="meta">Sets enterprise-sites-directory to TABLE_NAME=missing-table, waits until the update is active, calls the sites URL, waits 60–90 seconds for the Lambda Errors metric, then runs inventory.</p>
    </div>
    <div class="step">
      <h3>Restore</h3>
      <button type="button" class="action quiet" data-action="/api/restore">Restore the directory</button>
      <p class="meta">Sets TABLE_NAME=enterprise-sites, waits for a clean metric period, then runs inventory. The KAN task is left open.</p>
    </div>
    <div class="result" id="directory-note">{directory_result}</div>
    <div class="step">
      <h3>Close, simulated</h3>
      <p class="say">Say: This is the lab, a simulated account, not the live site directory. Walk the result as unhealthy, then the task, then the redrive, then verified telemetry, then Done.</p>
      <ol>
        <li>Unhealthy — the lab starts from a dead-letter queue that is not healthy.</li>
        <li>Task — recommendation mode proposes sqs.redrive_dlq and does not repair. On the live account that open problem is the Jira task, and it stays open.</li>
        <li>Redrive — the automatic check calls the redrive API.</li>
        <li>Verified — recovery counts only after fresh telemetry, not when the API call returns.</li>
        <li>Done — the live agent moves the Jira issue to Done only after that verified recovery. This lab does not call Jira.</li>
      </ol>
      <button type="button" class="action" data-action="/api/close">Show the close</button>
      {lab_meta}
      <pre id="lab-output">{lab_block}</pre>
    </div>
  </section>

  <section id="panel-catalog">
    <h2>3. New service type from Confluence</h2>
    <p class="say">Say: A new service is a heading on the catalog page, not a code change. The EKS form is {html.escape(EKS_HEADING)}. This account has no cluster, so SNS is the live stand-in. I will paste this table onto page 360481, create a topic, and scan.</p>
    <p>{catalog_html}. SRE_SERVICE_LIST_URI stays unset. {"The heading is already on the page. Paste only if it was removed. A second SNS-S01 row makes the catalog fail." if "sns:topic" in demo.catalog_types else "The heading is not on the page yet. Paste the table before the scan."}</p>
    <p>EKS heading, for the story: <strong>{html.escape(EKS_HEADING)}</strong>. SNS is the live stand-in because an EKS cluster is not in this account.</p>
    <div class="paste">
      <h3>{html.escape(SNS_HEADING)}</h3>
      <table>
        <thead><tr>{header_cells}</tr></thead>
        <tbody><tr>{row_cells}</tr></tbody>
      </table>
    </div>
    <p>
      <button type="button" class="copy" id="copy-catalog">Copy for Confluence</button>
    </p>
    <details>
      <summary>Exact markup to paste</summary>
      <pre id="catalog-html">{html.escape(storage)}</pre>
    </details>
    <pre id="catalog-plain" hidden>{html.escape(plain)}</pre>
    <div class="step">
      <button type="button" class="action" data-action="/api/topic">Create the topic and scan</button>
      <p class="meta">Creates SNS topic demo-alerts in us-east-2, tags it so the service list can see it, then runs inventory.</p>
    </div>
    <div class="result" id="topic-note">{topic}</div>
  </section>
  <footer>
    <p>Restart if this page stops: <code>{restart}</code></p>
  </footer>
</main>
<script>
const statusEl = document.getElementById("status");
async function poll() {{
  try {{
    const response = await fetch("/api/status");
    const body = await response.json();
    statusEl.textContent = body.error ? (body.message + " " + body.error) : body.message;
    statusEl.parentElement.className = body.error ? "status bad" : "status";
  }} catch (err) {{
    statusEl.textContent = "The status request failed.";
  }}
}}
async function run(action) {{
  document.querySelectorAll("button.action").forEach(button => {{ button.disabled = true; }});
  statusEl.textContent = "Starting.";
  const timer = setInterval(poll, 1000);
  try {{
    const response = await fetch(action, {{ method: "POST" }});
    clearInterval(timer);
    if (response.status === 409) {{
      const body = await response.json();
      statusEl.textContent = body.error || "Another action is still running.";
      document.querySelectorAll("button.action").forEach(button => {{ button.disabled = false; }});
      return;
    }}
    location.reload();
  }} catch (err) {{
    clearInterval(timer);
    statusEl.textContent = "The browser lost the connection to the demo server.";
    document.querySelectorAll("button.action").forEach(button => {{ button.disabled = false; }});
  }}
}}
document.querySelectorAll("button.action").forEach(button => {{
  button.addEventListener("click", () => run(button.dataset.action));
}});
document.getElementById("copy-catalog").addEventListener("click", async () => {{
  const markup = document.getElementById("catalog-html").textContent;
  const plain = document.getElementById("catalog-plain").textContent;
  try {{
    await navigator.clipboard.write([
      new ClipboardItem({{
        "text/html": new Blob([markup], {{ type: "text/html" }}),
        "text/plain": new Blob([plain], {{ type: "text/plain" }}),
      }}),
    ]);
    statusEl.textContent = "Copied the SNS heading and symptom table.";
  }} catch (err) {{
    statusEl.textContent = "Clipboard copy failed. Select the markup under Exact markup to paste.";
  }}
}});
</script>
</body>
</html>
"""


class DemoServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_handler(demo: Demo):
    routes = {
        "/api/add": demo.add_resource,
        "/api/remove": demo.remove_resource,
        "/api/break": demo.break_directory,
        "/api/restore": demo.restore_directory,
        "/api/close": demo.show_close,
        "/api/topic": demo.create_topic,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                page = render(demo).encode("utf-8")
                self._send(200, page, "text/html; charset=utf-8")
                return
            if path == "/api/status":
                payload = json.dumps(demo.status()).encode("utf-8")
                self._send(200, payload, "application/json")
                return
            if path == "/favicon.ico":
                self._send(204, b"", "text/plain")
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            action = routes.get(path)
            if action is None:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if not demo.try_begin():
                body = json.dumps({"ok": False, "error": "Another action is still running."}).encode("utf-8")
                self._send(409, body, "application/json")
                return
            try:
                action()
            except Exception as exc:
                demo.fail(exc)
                body = json.dumps({"ok": False, "error": demo.error}).encode("utf-8")
                self._send(500, body, "application/json")
                return
            body = json.dumps({"ok": True}).encode("utf-8")
            self._send(200, body, "application/json")

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
    _load_local_env()
    os.environ.pop("SRE_SERVICE_LIST_URI", None)
    _catalog_example_parses()
    demo = Demo()
    try:
        refresh_aws_login()
        demo.run_baseline()
    except Exception as exc:
        demo.fail(exc)
        demo.message = "Baseline inventory failed."
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
