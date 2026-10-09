"""Local click-through for the SRE health checker.

Shells out to python -m sre_agent and the AWS CLI. Does not reimplement the checker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from demo_ui.report_parse import diagnoses_for, parse_lab, parse_report, resource_named  # noqa: E402

HOST = "127.0.0.1"
PORT = 8765
ACCOUNT = "414279120882"
REGION = "us-east-2"
SITES_URL = "https://af38fctvq2.execute-api.us-east-2.amazonaws.com/prod/sites"
EXPECTED_SITES = ["SITE-1001", "SITE-1008", "SITE-2044", "SITE-3102", "SITE-4410"]
FUNCTION = "enterprise-sites-directory"
DEMO_TABLE = "demo-extra-sites"
DEMO_TOPIC = "demo-alerts"
MAIL_TO = "shruti.goyal@sjsu.edu"
PAGE_ID = "360481"
INVENTORY_TIMEOUT = 720
AWS_TIMEOUT = 180

SNS_SECTION = """
<h2>Service: SNS (sns:topic)</h2>
<p><strong>DEMO SECTION.</strong> Presentation only. This heading is enough for the checker to inventory SNS topics. There is no repair for this type.</p>
<table><tbody>
<tr><th>Symptom ID</th><th>Metric / query</th><th>Condition</th><th>Threshold</th><th>Status</th><th>Type</th><th>Namespace / log group</th><th>Dimension</th><th>Stat</th><th>Window (min)</th><th>Role</th></tr>
<tr><td>SNS-S01</td><td>NumberOfNotificationsFailed</td><td>&gt;</td><td>0</td><td>Active</td><td>metric</td><td>AWS/SNS</td><td>TopicName={name}</td><td>Sum</td><td>5</td><td>primary</td></tr>
</tbody></table>
"""

ENV = os.environ.copy()
ENV["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + ENV.get("PYTHONPATH", "")
ENV["PYTHONUNBUFFERED"] = "1"
STATE_PATH = Path(__file__).resolve().parent / "demo_state.json"
INDEX = Path(__file__).resolve().parent / "index.html"

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_ready: dict = {"sns_section": False, "sns_message": "", "mail_note": "", "table_name": ""}


def _tail(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return "The command failed."
    return lines[-1][:600]


def load_cli_credentials() -> None:
    """Give the checker process the same login the AWS CLI already has, without printing it."""
    code, out, err = run_command(
        ["aws", "configure", "export-credentials", "--format", "env"],
        60,
        lambda _message: None,
        "Reading AWS credentials",
    )
    if code != 0:
        raise RuntimeError(_tail(err or "The AWS CLI could not export credentials."))
    found = False
    for line in out.splitlines():
        text = line.strip()
        if text.startswith("export "):
            text = text[7:]
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key.startswith("AWS_") and value:
            ENV[key] = value
            found = True
    ENV["AWS_DEFAULT_REGION"] = REGION
    if not found:
        raise RuntimeError("The AWS CLI did not provide credentials for the checker.")


def redact(text: str) -> str:
    cleaned = text or ""
    for pool in (os.environ, ENV):
        for key, value in pool.items():
            upper = key.upper()
            if not value or len(value) < 8:
                continue
            if any(part in upper for part in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "GEMINI", "AWS_ACCESS", "AWS_SESSION")):
                cleaned = cleaned.replace(value, "[redacted]")
    return cleaned


def _prepare_args(args: list[str]) -> list[str]:
    if args[0] != "aws":
        return args
    found = shutil.which("aws")
    if not found:
        raise RuntimeError("The AWS CLI is not on PATH.")
    if os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", subprocess.list2cmdline([found, *args[1:]])]
    return [found, *args[1:]]


def run_command(args: list[str], timeout: int, progress, label: str) -> tuple[int, str, str]:
    args = _prepare_args(args)
    started = time.time()
    proc = subprocess.Popen(
        args,
        cwd=ROOT,
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    while True:
        try:
            out, err = proc.communicate(timeout=2)
            break
        except subprocess.TimeoutExpired:
            elapsed = int(time.time() - started)
            progress(f"{label} ({elapsed}s)")
            if elapsed >= timeout:
                proc.kill()
                proc.communicate()
                raise TimeoutError(f"{label} exceeded {timeout}s")
    return proc.returncode or 0, out or "", redact(err or "")


def aws_json(args: list[str], timeout: int = AWS_TIMEOUT) -> dict:
    code, out, err = run_command(["aws", *args, "--output", "json"], timeout, lambda _message: None, "aws")
    if code != 0:
        raise RuntimeError(redact(err or out or "AWS CLI failed"))
    return json.loads(out or "{}")


def progress_of(job: dict):
    def emit(message: str) -> None:
        with _jobs_lock:
            job["progress"].append(message)
            job["progress"] = job["progress"][-12:]
            job["current"] = message

    return emit


def load_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(updates: dict) -> None:
    state = load_state()
    state.update(updates)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def send_mail(subject: str, text: str) -> dict:
    """Send through SES in us-east-2 via the AWS CLI. A failed call is reported, never treated as sent."""
    payload = {
        "FromEmailAddress": MAIL_TO,
        "Destination": {"ToAddresses": [MAIL_TO]},
        "Content": {
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": text, "Charset": "UTF-8"}},
            }
        },
    }
    path = Path(__file__).resolve().parent / "_mail_request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        code, out, err = run_command(
            ["aws", "sesv2", "send-email", "--region", REGION, "--cli-input-json", "file://demo_ui/_mail_request.json"],
            60,
            lambda _message: None,
            "Sending mail",
        )
    finally:
        path.unlink(missing_ok=True)
    if code != 0:
        return {"sent": False, "detail": redact(err or out or "SES send failed")}
    try:
        message_id = json.loads(out or "{}").get("MessageId", "")
    except json.JSONDecodeError:
        message_id = ""
    return {"sent": True, "detail": f"Sent to {MAIL_TO}. Message {message_id}."}


def mail_body(what: str, resource: str, health: str, jira: str, extra: str = "") -> str:
    lines = [
        what,
        f"Resource: {resource}",
        f"Health: {health}",
        f"Jira: {jira or 'none'}",
        f"Region: {REGION}",
        f"Account: {ACCOUNT}",
    ]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def ensure_ses_identity() -> str:
    code, out, err = run_command(
        ["aws", "sesv2", "get-email-identity", "--email-identity", MAIL_TO, "--region", REGION, "--output", "json"],
        60,
        lambda _message: None,
        "Checking SES",
    )
    if code != 0 and "NotFoundException" in err:
        code, _out, err = run_command(
            ["aws", "sesv2", "create-email-identity", "--email-identity", MAIL_TO, "--region", REGION],
            60,
            lambda _message: None,
            "Requesting SES verification",
        )
        if code != 0:
            return redact(err or "Could not request SES verification")
        return (
            f"Amazon SES is in the sandbox. A verification message was sent to {MAIL_TO}. "
            "Click that Amazon link before mail can be delivered."
        )
    if code != 0:
        return redact(err or "Could not read the SES identity")
    identity = json.loads(out or "{}")
    if identity.get("VerifiedForSendingStatus"):
        return f"{MAIL_TO} is verified in Amazon SES."
    return (
        f"Amazon SES is in the sandbox and {MAIL_TO} is not verified yet. "
        "Click the Amazon verification link already sent to that address. Until then, each popup shows the send error."
    )


def _atlassian(kind: str):
    from sre_agent.cli import _load_local_env
    from sre_agent.integrations.secrets import secret_value

    _load_local_env()
    prefix = "CONFLUENCE" if kind == "confluence" else "JIRA"
    base = (secret_value(f"{prefix}_BASE_URL", f"{prefix}_SECRET_ARN") or "").rstrip("/")
    email = secret_value(f"{prefix}_EMAIL", f"{prefix}_SECRET_ARN") or ""
    token = secret_value(f"{prefix}_TOKEN", f"{prefix}_SECRET_ARN") or ""
    if not (base and email and token):
        raise RuntimeError(f"{kind} is not configured")
    return base, email, token


def _http_json(method: str, url: str, email: str, token: str, body: dict | None = None) -> dict:
    import base64

    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    raw = base64.b64encode(f"{email}:{token}".encode()).decode()
    request.add_header("Authorization", f"Basic {raw}")
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(redact(f"HTTP {exc.code}: {detail}")) from None
    return json.loads(payload) if payload else {}


def ensure_sns_section() -> str:
    from sre_agent.integrations.confluence import parse_rule_table, validate_rules

    base, email, token = _atlassian("confluence")
    url = f"{base}/wiki/api/v2/pages/{PAGE_ID}?body-format=storage"
    page = _http_json("GET", url, email, token)
    storage = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    title = page.get("title") or "AWS failure scenario catalog"
    version = int((page.get("version") or {}).get("number") or 0)
    if "Service: SNS (sns:topic)" in storage:
        rules = parse_rule_table(storage, version)
        errors = validate_rules(rules)
        if errors:
            raise RuntimeError("The page already has an SNS heading, but the catalog does not validate.")
        return "The SNS demo section is already on the Confluence page."
    current_rules = parse_rule_table(storage, version)
    current_errors = validate_rules(current_rules)
    if current_errors:
        raise RuntimeError("The current Confluence catalog does not validate, so it was left unchanged.")
    updated = storage + SNS_SECTION
    rules = parse_rule_table(updated, version + 1)
    errors = validate_rules(rules)
    if errors or not any(rule.resource_type == "sns:topic" and rule.rule_id == "SNS-S01" for rule in rules):
        raise RuntimeError("The SNS section did not parse, so the page was not changed.")
    def _put(body: str, number: int, message: str) -> None:
        _http_json(
            "PUT",
            f"{base}/wiki/api/v2/pages/{PAGE_ID}",
            email,
            token,
            {
                "id": PAGE_ID,
                "status": "current",
                "title": title,
                "body": {"representation": "storage", "value": body},
                "version": {"number": number, "message": message},
            },
        )

    _put(updated, version + 1, "Add the SNS demo section")
    checked = _http_json("GET", url, email, token)
    checked_storage = ((checked.get("body") or {}).get("storage") or {}).get("value") or ""
    checked_version = int((checked.get("version") or {}).get("number") or 0)
    try:
        checked_rules = parse_rule_table(checked_storage, checked_version)
        checked_errors = validate_rules(checked_rules)
    except Exception as exc:
        checked_errors = [redact(str(exc))]
        checked_rules = []
    sns_ok = any(rule.resource_type == "sns:topic" and rule.rule_id == "SNS-S01" for rule in checked_rules)
    if checked_errors or not sns_ok:
        _put(storage, checked_version + 1, "Restore the catalog after the SNS section did not validate")
        raise RuntimeError("The SNS section did not survive on the page, so the previous catalog was restored.")
    return "Added the SNS demo section to the Confluence page."


def function_config() -> dict:
    return aws_json(
        ["lambda", "get-function-configuration", "--function-name", FUNCTION, "--region", REGION]
    )


def set_table_name(value: str, progress) -> str:
    if value not in {"enterprise-sites", "missing-table"}:
        raise RuntimeError("Refusing to set TABLE_NAME to anything except the demo values.")
    current = function_config()
    variables = dict((current.get("Environment") or {}).get("Variables") or {})
    variables["TABLE_NAME"] = value
    progress(f"Setting TABLE_NAME={value}")
    environment = "Variables={" + ",".join(f"{key}={variables[key]}" for key in sorted(variables)) + "}"
    code, _out, err = run_command(
        [
            "aws",
            "lambda",
            "update-function-configuration",
            "--function-name",
            FUNCTION,
            "--region",
            REGION,
            "--environment",
            environment,
        ],
        AWS_TIMEOUT,
        progress,
        "Updating the function",
    )
    if code != 0:
        raise RuntimeError(err or "Could not update the function")
    progress("Waiting until the function update is active")
    code, _out, err = run_command(
        ["aws", "lambda", "wait", "function-updated", "--function-name", FUNCTION, "--region", REGION],
        AWS_TIMEOUT,
        progress,
        "Waiting for the function",
    )
    if code != 0:
        raise RuntimeError(err or "The function update did not become active")
    confirmed = ((function_config().get("Environment") or {}).get("Variables") or {}).get("TABLE_NAME")
    if confirmed != value:
        raise RuntimeError("TABLE_NAME did not stick")
    return confirmed


def invoke_sites(times: int, progress) -> dict:
    codes = []
    body = ""
    for number in range(times):
        progress(f"Calling the sites API ({number + 1} of {times})")
        request = urllib.request.Request(SITES_URL)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                codes.append(response.status)
                body = response.read().decode()
        except urllib.error.HTTPError as exc:
            codes.append(exc.code)
            body = exc.read().decode(errors="replace")
        time.sleep(1)
    site_ids = []
    try:
        payload = json.loads(body)
        site_ids = sorted(str(item.get("site_id")) for item in payload.get("sites") or [] if item.get("site_id"))
    except json.JSONDecodeError:
        site_ids = []
    return {"codes": codes, "site_ids": site_ids}


def metric_datapoints(metric: str) -> list[dict]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=20)
    payload = aws_json(
        [
            "cloudwatch",
            "get-metric-statistics",
            "--namespace",
            "AWS/Lambda",
            "--metric-name",
            metric,
            "--dimensions",
            f"Name=FunctionName,Value={FUNCTION}",
            "--start-time",
            start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--end-time",
            end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--period",
            "60",
            "--statistics",
            "Sum",
            "--region",
            REGION,
        ]
    )
    return payload.get("Datapoints") or []


def wait_for_errors(progress) -> str:
    deadline = time.time() + 180
    while time.time() < deadline:
        points = metric_datapoints("Errors")
        total = sum(float(point.get("Sum") or 0) for point in points)
        progress(f"Waiting for the Errors metric ({int(deadline - time.time())}s left, sum {total:.0f})")
        if total > 0:
            return f"Errors metric sum is {total:.0f} over the recent minute buckets."
        time.sleep(15)
    return "The Errors metric had not published a non-zero sum after three minutes. Inventory still runs."


def wait_for_fresh_success(progress) -> str:
    deadline = time.time() + 180
    while time.time() < deadline:
        errors = metric_datapoints("Errors")
        invocations = metric_datapoints("Invocations")
        latest_error = max(errors, key=lambda point: point.get("Timestamp", ""), default=None)
        latest_invoke = max(invocations, key=lambda point: point.get("Timestamp", ""), default=None)
        error_sum = float((latest_error or {}).get("Sum") or 0)
        invoke_sum = float((latest_invoke or {}).get("Sum") or 0)
        invoke_at = str((latest_invoke or {}).get("Timestamp") or "")
        error_at = str((latest_error or {}).get("Timestamp") or "")
        progress(
            f"Waiting for a successful period ({int(deadline - time.time())}s left, "
            f"latest errors {error_sum:.0f}, invocations {invoke_sum:.0f})"
        )
        fresh = bool(latest_invoke) and invoke_sum > 0 and (not latest_error or invoke_at > error_at or (invoke_at == error_at and error_sum == 0))
        if fresh:
            return "The latest published period has successful invocations and no errors."
        time.sleep(15)
    return "A clean metric period was not published in three minutes. Inventory still runs."


def run_inventory(progress) -> dict:
    progress("Running python -m sre_agent inventory")
    load_cli_credentials()
    code, out, err = run_command(
        [sys.executable, "-m", "sre_agent", "inventory", "--account", ACCOUNT, "--region", REGION],
        INVENTORY_TIMEOUT,
        progress,
        "Running inventory",
    )
    if code != 0:
        raise RuntimeError(_tail(err or out or "Inventory failed"))
    return parse_report(out)


def guard(name: str) -> None:
    if name == "enterprise-sites" or name.startswith("sre-agent"):
        raise RuntimeError(f"Refusing to change {name}")


def create_demo_table(progress) -> None:
    guard(DEMO_TABLE)
    progress(f"Creating DynamoDB table {DEMO_TABLE}")
    code, out, err = run_command(
        [
            "aws",
            "dynamodb",
            "create-table",
            "--table-name",
            DEMO_TABLE,
            "--attribute-definitions",
            "AttributeName=site_id,AttributeType=S",
            "--key-schema",
            "AttributeName=site_id,KeyType=HASH",
            "--billing-mode",
            "PAY_PER_REQUEST",
            "--region",
            REGION,
        ],
        AWS_TIMEOUT,
        progress,
        "Creating the table",
    )
    if code != 0 and "ResourceInUseException" not in err:
        raise RuntimeError(err or out or "Could not create the table")
    progress("Waiting until the table is active")
    code, _out, err = run_command(
        ["aws", "dynamodb", "wait", "table-exists", "--table-name", DEMO_TABLE, "--region", REGION],
        AWS_TIMEOUT,
        progress,
        "Waiting for the table",
    )
    if code != 0:
        raise RuntimeError(err or "The table did not become active")


def delete_demo_table(progress) -> str:
    guard(DEMO_TABLE)
    progress(f"Deleting DynamoDB table {DEMO_TABLE}")
    code, _out, err = run_command(
        ["aws", "dynamodb", "delete-table", "--table-name", DEMO_TABLE, "--region", REGION],
        AWS_TIMEOUT,
        progress,
        "Deleting the table",
    )
    if code != 0 and "ResourceNotFoundException" in err:
        return "The table was already gone."
    if code != 0:
        raise RuntimeError(err or "Could not delete the table")
    progress("Waiting until the table is gone")
    code, _out, err = run_command(
        ["aws", "dynamodb", "wait", "table-not-exists", "--table-name", DEMO_TABLE, "--region", REGION],
        AWS_TIMEOUT,
        progress,
        "Waiting for deletion",
    )
    if code != 0:
        raise RuntimeError(err or "The table delete did not finish")
    return "Deleted demo-extra-sites."


def create_demo_topic(progress) -> str:
    guard(DEMO_TOPIC)
    progress(f"Creating SNS topic {DEMO_TOPIC}")
    payload = aws_json(
        [
            "sns",
            "create-topic",
            "--name",
            DEMO_TOPIC,
            "--tags",
            "Key=sre-demo,Value=presentation",
            "--region",
            REGION,
        ]
    )
    arn = payload.get("TopicArn", "")
    if not arn.endswith(f":{DEMO_TOPIC}"):
        raise RuntimeError("SNS did not return the demo topic")
    deadline = time.time() + 90
    while time.time() < deadline:
        progress(f"Waiting until the tagging API can see {DEMO_TOPIC}")
        listed = aws_json(
            [
                "resourcegroupstaggingapi",
                "get-resources",
                "--resource-type-filters",
                "sns:topic",
                "--region",
                REGION,
            ]
        )
        for item in listed.get("ResourceTagMappingList") or []:
            if str(item.get("ResourceARN", "")).endswith(f":{DEMO_TOPIC}"):
                return arn
        time.sleep(5)
    return arn


def delete_demo_topic(progress) -> str:
    guard(DEMO_TOPIC)
    progress(f"Deleting SNS topic {DEMO_TOPIC} if it exists")
    payload = aws_json(["sns", "list-topics", "--region", REGION])
    arn = ""
    for item in payload.get("Topics") or []:
        candidate = str(item.get("TopicArn") or "")
        if candidate == f"arn:aws:sns:{REGION}:{ACCOUNT}:{DEMO_TOPIC}":
            arn = candidate
            break
    if not arn:
        return "demo-alerts was already gone."
    code, _out, err = run_command(
        ["aws", "sns", "delete-topic", "--topic-arn", arn, "--region", REGION],
        AWS_TIMEOUT,
        progress,
        "Deleting the topic",
    )
    if code != 0:
        raise RuntimeError(err or "Could not delete the topic")
    return "Deleted demo-alerts."


def jira_request(method: str, path: str, body: dict | None = None) -> dict:
    base, email, token = _atlassian("jira")
    return _http_json(method, base + path, email, token, body)


def issue_view(issue: dict) -> dict:
    fields = issue.get("fields") or {}
    status = (fields.get("status") or {}).get("name") or ""
    category = ((fields.get("status") or {}).get("statusCategory") or {}).get("key") or ""
    return {"key": issue.get("key") or "", "status": status, "category": category, "summary": fields.get("summary") or ""}


def fetch_issue(key: str) -> dict:
    if not key:
        return {"key": "", "status": "", "category": "", "summary": ""}
    payload = jira_request("GET", f"/rest/api/3/issue/{key}?fields=status,summary")
    return issue_view(payload)


def find_lambda_issue(function_arn: str) -> dict:
    from sre_agent.integrations.jira import _label

    for code in ("lambda.errors", "lambda.errors_after_deploy", "lambda.throttled"):
        label = _label(f"{ACCOUNT}|{REGION}|{function_arn}|{code}")
        payload = jira_request(
            "POST",
            "/rest/api/3/search/jql",
            {
                "jql": f'project = "KAN" AND labels = "sre-agent" AND labels = "{label}" ORDER BY created DESC',
                "maxResults": 2,
                "fields": ["key", "summary", "status"],
            },
        )
        issues = payload.get("issues") or []
        if issues:
            found = issue_view(issues[0])
            save_state({"jira_key": found["key"]})
            return found
    stored = load_state().get("jira_key") or ""
    if stored:
        return fetch_issue(stored)
    return {"key": "", "status": "", "category": "", "summary": ""}


def highlight(label: str, value: str) -> dict:
    return {"label": label, "value": value}


def result(title: str, narration: str, highlights: list[dict], mail: dict, note: str = "") -> dict:
    return {"title": title, "narration": narration, "highlights": highlights, "mail": mail, "note": note}


def action_demo1_create(progress) -> dict:
    create_demo_table(progress)
    report = run_inventory(progress)
    found = resource_named(report, DEMO_TABLE, "dynamodb:table")
    detected = found is not None and found["presence"] == "present"
    health = found["health"] if found else "not listed"
    mail = send_mail(
        "SRE agent: demo-extra-sites detected",
        mail_body(
            "Inventory detected a DynamoDB table that was just created." if detected else "Inventory did not list the new table.",
            DEMO_TABLE,
            health,
            "",
        ),
    )
    return result(
        "The new table was detected" if detected else "The new table was not listed",
        "Inventory lists demo-extra-sites as a live DynamoDB table."
        if detected
        else "The table was created, but this inventory pass did not list it as present.",
        [
            highlight("Resource", DEMO_TABLE),
            highlight("Presence", found["presence"] if found else "absent"),
            highlight("Health", health),
            highlight("Type", "dynamodb:table"),
        ],
        mail,
    )


def action_demo1_delete(progress) -> dict:
    note = delete_demo_table(progress)
    report = run_inventory(progress)
    found = resource_named(report, DEMO_TABLE, "dynamodb:table")
    gone = found is None or found["presence"] == "deleted"
    health = "gone" if found is None else found["health"]
    mail = send_mail(
        "SRE agent: demo-extra-sites removed",
        mail_body(
            "Inventory no longer lists demo-extra-sites as a live resource." if gone else "Inventory still lists demo-extra-sites.",
            DEMO_TABLE,
            health,
            "",
        ),
    )
    return result(
        "The table is no longer live" if gone else "The table is still listed",
        "Inventory no longer lists demo-extra-sites as a live resource." if gone else "The delete finished, but the table is still in the inventory.",
        [
            highlight("Resource", DEMO_TABLE),
            highlight("In inventory", "no" if found is None else found["presence"]),
            highlight("Cleanup", note),
        ],
        mail,
    )


def action_demo2_break(progress) -> dict:
    set_table_name("missing-table", progress)
    calls = invoke_sites(4, progress)
    metric_note = wait_for_errors(progress)
    report = run_inventory(progress)
    found = resource_named(report, FUNCTION, "lambda:function")
    health = found["health"] if found else "not listed"
    diagnoses = diagnoses_for(report, FUNCTION)
    problem = diagnoses[0]["problem_code"] if diagnoses else "none"
    jira_key = next((item["jira"] for item in diagnoses if item["jira"]), "")
    issue = {"key": jira_key, "status": ""}
    lookup_note = ""
    try:
        arn = function_config().get("FunctionArn") or ""
        issue = find_lambda_issue(arn) if not jira_key else fetch_issue(jira_key)
        if issue.get("key"):
            save_state({"jira_key": issue["key"]})
    except Exception as exc:
        lookup_note = redact(str(exc))
    key = issue.get("key") or jira_key
    mail = send_mail(
        f"SRE agent: {FUNCTION} unhealthy",
        mail_body(
            "The directory Lambda is unhealthy and a KAN task was opened.",
            FUNCTION,
            health,
            key or "none",
            f"Problem: {problem}",
        ),
    )
    highlights = [
        highlight("Resource", FUNCTION),
        highlight("Health", health),
        highlight("Problem", problem),
        highlight("Jira", key or "not found"),
        highlight("Jira status", issue.get("status") or "unknown"),
        highlight("API codes", ", ".join(str(code) for code in calls["codes"])),
        highlight("Errors metric", metric_note),
    ]
    return result(
        "A Jira task is open" if key else "The function was checked",
        "The directory function is unhealthy, and the checker opened a KAN task."
        if key
        else "Inventory finished. The KAN key was not on the report.",
        highlights,
        mail,
        lookup_note,
    )


def action_demo2_restore(progress) -> dict:
    set_table_name("enterprise-sites", progress)
    progress("Giving CloudWatch a fresh minute after the failure")
    time.sleep(20)
    calls = invoke_sites(4, progress)
    metric_note = wait_for_fresh_success(progress)
    report = run_inventory(progress)
    found = resource_named(report, FUNCTION, "lambda:function")
    health = found["health"] if found else "not listed"
    key = load_state().get("jira_key") or ""
    issue = {"key": key, "status": "", "category": ""}
    lookup_note = ""
    try:
        issue = fetch_issue(key) if key else find_lambda_issue(function_config().get("FunctionArn") or "")
        key = issue.get("key") or key
    except Exception as exc:
        lookup_note = redact(str(exc))
    still_open = bool(key) and issue.get("category") != "done" and issue.get("status").lower() != "done"
    sites = ", ".join(calls["site_ids"]) if calls["site_ids"] else "not returned"
    mail = send_mail(
        f"SRE agent: {FUNCTION} healthy, task still open",
        mail_body(
            "The directory function was restored. The KAN task is still open." if still_open else "The directory was restored. Check the Jira status in the popup.",
            FUNCTION,
            health,
            key or "none",
            f"Sites: {sites}",
        ),
    )
    return result(
        "Healthy, and the task is still open" if health == "healthy" and still_open else "Restore result",
        "The function is healthy again and the five sites are back, while the KAN task remains open."
        if health == "healthy" and still_open
        else "Inventory and Jira were read after the restore. The live values are below.",
        [
            highlight("Resource", FUNCTION),
            highlight("Health", health),
            highlight("TABLE_NAME", "enterprise-sites"),
            highlight("Sites", sites),
            highlight("Jira", key or "not found"),
            highlight("Jira status", issue.get("status") or "unknown"),
            highlight("Still open", "yes" if still_open else "no"),
            highlight("Metric", metric_note),
        ],
        mail,
        lookup_note,
    )


def action_demo2_lab(progress) -> dict:
    progress("Running python -m sre_agent lab")
    code, out, err = run_command(
        [sys.executable, "-m", "sre_agent", "lab"],
        180,
        progress,
        "Running the lab",
    )
    parsed = parse_lab(out)
    if code != 0 and not parsed["checks"]:
        raise RuntimeError(err or "The lab failed to start")
    passed = code == 0 and parsed["passed"]
    mail = send_mail(
        "SRE agent: lab close verified" if passed else "SRE agent: lab close did not pass",
        mail_body(
            "The simulated repair was verified and the task moved to Done."
            if passed
            else "The lab did not verify the simulated close.",
            "payments-dlq",
            "recovered" if passed else "not recovered",
            "simulated",
            "This close is the local lab. The live KAN issue is not moved.",
        ),
    )
    by_name = {item["name"]: item for item in parsed["checks"]}

    def line_for(fragment: str) -> str:
        for name, item in by_name.items():
            if fragment in name:
                return f"{item['mark']}  {name}: {item['detail']}"
        return "That lab line was not in the output."

    walk = [
        {
            "title": "Unhealthy queue",
            "narration": "The simulated queue is holding orders that failed, and recommend mode leaves them there.",
            "highlights": [highlight("Lab", line_for("recommendation mode"))],
        },
        {
            "title": "Task opened",
            "narration": "The open fault is the task the checker is tracking. This lab does not create a live KAN issue.",
            "highlights": [highlight("Lab", line_for("recommendation mode"))],
        },
        {
            "title": "Transient redrive",
            "narration": "The lab replays only the transient failure.",
            "highlights": [highlight("Lab", line_for("transient redrive"))],
        },
        {
            "title": "Order processed",
            "narration": "Fresh telemetry shows the replayed order was processed.",
            "highlights": [highlight("Lab", line_for("transient redrive"))],
        },
        {
            "title": "Task moved to Done",
            "narration": "Verified recovery moves the simulated task to Done. The live KAN issue stays open.",
            "highlights": [highlight("Lab", "All checks passed." if passed else "The lab did not pass.")],
        },
    ]
    return {
        "title": walk[0]["title"],
        "narration": walk[0]["narration"],
        "highlights": walk[0]["highlights"],
        "mail": mail,
        "note": "",
        "walk": walk,
    }


def action_demo3_create(progress) -> dict:
    create_demo_topic(progress)
    report = run_inventory(progress)
    found = resource_named(report, DEMO_TOPIC, "sns:topic")
    diagnoses = diagnoses_for(report, DEMO_TOPIC)
    problem = next((item["problem_code"] for item in diagnoses), "none")
    action = next((item["action"] for item in diagnoses), "none")
    source = found["source"] if found else ""
    scan = next((item for item in report["scans"] if item["collector"] == "service-list:sns:topic"), None)
    if scan and not source:
        source = "service-list"
    health = found["health"] if found else "not listed"
    breach = problem == "catalog.symptom"
    mail = send_mail(
        "SRE agent: demo-alerts detected",
        mail_body(
            "A new SNS service type from Confluence was inventoried.",
            DEMO_TOPIC,
            health,
            "",
            f"Source: {source or 'not service-list'}. Problem: {problem}. Repair: none.",
        ),
    )
    note = "EKS is the same heading shape: Service: EKS (eks:cluster). It needs a real cluster before inventory can show one."
    return result(
        "demo-alerts came from the service list" if source == "service-list" else "Topic scan finished",
        "Inventory discovered demo-alerts with source service-list. There is no repair for this type."
        if source == "service-list"
        else "The topic was created. This inventory pass did not mark the source as service-list.",
        [
            highlight("Resource", DEMO_TOPIC),
            highlight("Type", "sns:topic"),
            highlight("Source", source or "not recorded"),
            highlight("Presence", found["presence"] if found else "absent"),
            highlight("Health", health),
            highlight("Problem", problem if breach or diagnoses else "no catalog breach"),
            highlight("Repair", "none" if action in {"", "none"} else action),
            highlight("Collector", scan["message"] if scan else "service-list scan not in the report"),
        ],
        mail,
        note,
    )


def action_reset(progress) -> dict:
    set_table_name("enterprise-sites", progress)
    table_note = delete_demo_table(progress)
    topic_note = delete_demo_topic(progress)
    calls = invoke_sites(1, progress)
    sites = ", ".join(calls["site_ids"]) if calls["site_ids"] else "not returned"
    return result(
        "The demo account is clean",
        "The directory reads enterprise-sites again, and the temporary demo resources are gone.",
        [
            highlight("TABLE_NAME", "enterprise-sites"),
            highlight("Table", table_note),
            highlight("Topic", topic_note),
            highlight("Sites", sites),
        ],
        {"sent": False, "detail": "Reset does not send mail."},
    )


ACTIONS = {
    "demo1_create": action_demo1_create,
    "demo1_delete": action_demo1_delete,
    "demo2_break": action_demo2_break,
    "demo2_restore": action_demo2_restore,
    "demo2_lab": action_demo2_lab,
    "demo3_create": action_demo3_create,
    "reset": action_reset,
}


def start_job(action: str) -> dict:
    if action not in ACTIONS:
        raise KeyError(action)
    job = {
        "id": uuid.uuid4().hex[:12],
        "action": action,
        "state": "running",
        "progress": ["Starting"],
        "current": "Starting",
        "result": None,
        "error": "",
    }
    with _jobs_lock:
        if any(item["state"] == "running" for item in _jobs.values()):
            raise RuntimeError("Another demo step is still running.")
        _jobs[job["id"]] = job

    def runner() -> None:
        try:
            finished = ACTIONS[action](progress_of(job))
            with _jobs_lock:
                job["result"] = finished
                job["state"] = "done"
                job["current"] = "Finished"
        except Exception as exc:
            with _jobs_lock:
                job["state"] = "error"
                job["error"] = _tail(redact(str(exc)))
                job["current"] = "Stopped"

    threading.Thread(target=runner, daemon=True).start()
    return {"id": job["id"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        message = redact(fmt % args)
        if "TOKEN" in message or "api_token" in message.lower():
            return
        sys.stderr.write("%s %s\n" % (self.address_string(), message))

    def _send(self, status: int, payload: dict | str, content_type: str) -> None:
        data = payload if isinstance(payload, bytes) else (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self._send(200, INDEX.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/ready":
            self._send(200, _ready, "application/json")
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with _jobs_lock:
                job = _jobs.get(job_id)
            if job is None:
                self._send(404, {"error": "No such step"}, "application/json")
                return
            self._send(200, job, "application/json")
            return
        self._send(404, {"error": "Not found"}, "application/json")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/jobs":
            self._send(404, {"error": "Not found"}, "application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
            started = start_job(str(body.get("action") or ""))
        except KeyError:
            self._send(400, {"error": "Unknown action"}, "application/json")
            return
        except RuntimeError as exc:
            self._send(409, {"error": redact(str(exc))}, "application/json")
            return
        self._send(202, started, "application/json")


def prepare() -> None:
    try:
        _ready["sns_message"] = ensure_sns_section()
        _ready["sns_section"] = "SNS demo section" in _ready["sns_message"] or "already" in _ready["sns_message"]
    except Exception as exc:
        _ready["sns_section"] = False
        _ready["sns_message"] = redact(str(exc))
    _ready["mail_note"] = ensure_ses_identity()
    try:
        table = ((function_config().get("Environment") or {}).get("Variables") or {}).get("TABLE_NAME", "")
        _ready["table_name"] = table
    except Exception as exc:
        _ready["table_name"] = redact(str(exc))


def main() -> None:
    prepare()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Demo UI at http://{HOST}:{PORT}", flush=True)
    print(redact(_ready["sns_message"]), flush=True)
    print(redact(_ready["mail_note"]), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        Path(__file__).resolve().parent.joinpath("crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
