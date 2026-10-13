from sre_agent.demo_plans import concrete_correction, resolved_proposal, ticket_decision, ticket_plan
from sre_agent.demo_ui import Demo, inventory_groups, render, render_jira, shown_health, type_label
from sre_agent.integrations.confluence import load_catalog, storage_has_service, workload_section
from sre_agent.integrations.confluence import FakeConfluence
from sre_agent.integrations.jira import _star_doc, parse_star_description, star_plain
from sre_agent.models import Diagnosis, ProposedAction


def _row(name, resource_type, health="healthy", code="", presence="present"):
    return {
        "name": name,
        "resource_type": resource_type,
        "health": health,
        "problem_code": code,
        "problem": "",
        "discovery_source": "api",
        "presence": presence,
    }


def test_groups_follow_found_resources_not_catalog_headings():
    rows = [
        _row("fn-a", "lambda:function"),
        _row("fn-b", "lambda:function", "unhealthy", "lambda.errors"),
        _row("fn-c", "lambda:function", "unknown", "telemetry.missing"),
        _row("gone", "lambda:function", presence="missing"),
        _row("sites", "dynamodb:table"),
        _row("jobs", "sqs:queue"),
        _row("edge", "apigateway:restapi"),
        _row("alarms", "sns:topic"),
        _row("box", "ec2:instance"),
        _row("payments", "eks:cluster", "unknown", "telemetry.missing"),
    ]
    groups = inventory_groups(rows)
    assert [(item["label"], len(item["rows"])) for item in groups] == [
        ("Lambda", 3),
        ("DynamoDB", 1),
        ("SQS", 1),
        ("API Gateway", 1),
        ("SNS", 1),
        ("EC2", 1),
        ("eks:cluster", 1),
    ]
    assert type_label("lambda:function") == "Lambda"
    assert "gone" not in {row["name"] for group in groups for row in group["rows"]}

    demo = Demo()
    demo.rows = rows
    demo.catalog_types = ["sns:topic"]
    page = render(demo)
    main = page
    assert "AWS workloads in this account right now" in main
    assert "Extra catalog types" not in page
    assert "Copy for Confluence" not in page
    assert "Show the close" not in page
    assert "Clean up" not in page
    assert "Create the topic" not in page
    assert "Delete the topic" not in page
    assert "Add a resource" not in page
    assert "Remove it" not in page
    assert "Add this service" not in page
    assert "catalog-form" not in page
    assert "Add a service" not in page
    assert "class='type-count'>3" in main
    assert "class='type-detail' hidden" in main
    assert "telemetry.missing" in main
    assert "fn-a" in main and "fn-b" in main and "fn-c" in main
    assert "SRE_SERVICE_LIST_URI" not in page
    assert ">unknown<" not in page
    assert "health unknown" not in page
    assert "class='health degraded'" in main
    assert 'id="facts-overlay" hidden' in page
    assert "· Region" not in page
    assert 'href="/jira"' in main
    assert ">Jira</a>" in main
    assert 'id="workload-form"' not in page
    assert "Populate" not in page
    assert 'id="conditions-open"' not in page
    assert 'id="conditions-overlay"' not in page
    assert "/api/catalog" not in page
    assert "Service name" not in page
    assert "each recipient must be verified" not in page
    assert page.index('id="panel-jira"') < page.index('id="panel-confluence"')
    assert page.index('id="panel-confluence"') < page.index("<h2>Mail</h2>")
    assert 'id="saved-addresses"' in page
    assert 'id="delete-email"' in page
    assert 'id="new-address"' in page
    assert "<textarea" not in page
    assert "delete-resource" not in page
    assert 'id="delete-overlay"' not in page
    assert "/api/delete" not in page
    demo.issue_views = [{
        "key": "KAN-1",
        "url": "https://example.test/browse/KAN-1",
        "status": "To Do",
        "situation": "Lambda is returning errors.",
        "proposed": "Set TABLE_NAME to enterprise-sites.",
        "note": "",
    }]
    jira = render_jira(demo)
    assert "Proposed solution" in jira
    assert "Where seen" in jira
    assert ">Issue</h3>" in jira
    assert "Take this action" in jira
    assert "Break the directory" not in page
    assert "Restore the directory" not in page
    assert "Directory health" not in page
    assert "Let me test how this works" not in page
    assert "Nothing changes until Verify and apply." not in page
    assert 'id="test-overlay"' not in page
    assert "detail.hidden = false" in page


def test_ticket_action_runs_only_known_repairs():
    live = "https://sqs.us-east-2.amazonaws.com/414279120882/sre-agent-orders"
    lab = "arn:aws:sqs:us-east-2:414279120882:lab-orders"
    refused = ticket_decision(
        "Move messages back. Runbook sqs.redrive_dlq.",
        resource_name="sre-agent-orders",
        resource_id=live,
        resource_type="sqs:queue",
        lab_targets={live, lab},
    )
    assert refused["run"] is False
    assert refused["message"] == "The proposed action is not one this page can run."
    allowed = ticket_decision(
        "Move messages back. Runbook sqs.redrive_dlq.",
        resource_name="lab-orders",
        resource_id=lab,
        resource_type="sqs:queue",
        lab_targets={lab},
    )
    assert allowed == {"kind": "redrive", "run": True, "target": lab, "message": ""}
    missing = ticket_decision("The queue is behind.", summary="Orders are slow.")
    assert missing["message"] == "The proposed action is not one this page can run."
    restore = ticket_decision(
        "Set TABLE_NAME to enterprise-sites.",
        resource_name="enterprise-sites-directory",
        resource_type="lambda:function",
    )
    assert restore["kind"] == "restore-directory"
    assert restore["run"] is True


def test_star_description_reads_headings_and_rejects_other_text():
    diagnosis = Diagnosis(
        incident_id="inc-1",
        resource_id="res-1",
        observed_problem="Lambda is returning errors.",
        problem_code="lambda.errors",
        health="unhealthy",
        proposed_action=ProposedAction("sqs.redrive_dlq", "res-1", "Move the messages.", ["ev-1"], False),
    )
    parsed = parse_star_description(star_plain(diagnosis))
    assert parsed is not None
    assert parsed["situation"].startswith("Lambda is returning errors.")
    assert parsed["task"]
    assert "sqs.redrive_dlq" in parsed["proposed"]
    adf = parse_star_description(_star_doc(diagnosis))
    assert adf is not None
    assert adf["situation"].startswith("Lambda is returning errors.")
    assert parse_star_description("A summary without headings.") is None


def test_health_word_is_never_unknown():
    assert shown_health("healthy") == "healthy"
    assert shown_health("unhealthy") == "unhealthy"
    assert shown_health("unknown") == "degraded"
    assert shown_health("") == "degraded"


def test_edited_ticket_text_is_the_verify_list():
    edited = "Set TABLE_NAME to enterprise-sites."
    decision = ticket_decision(edited, resource_name="enterprise-sites-directory", resource_type="lambda:function")
    plan = ticket_plan("KAN-4", decision, "us-east-2", proposed=edited)
    assert plan["will_run"] is True
    assert any("TABLE_NAME to enterprise-sites" in item for item in plan["items"])
    refused = ticket_plan(
        "KAN-4",
        ticket_decision("Delete the production table."),
        "us-east-2",
        proposed="Delete the production table.",
    )
    assert refused["will_run"] is False
    assert "was not run" not in " ".join(refused["items"]).lower()
    assert "was not run" not in refused["lead"].lower()


def test_workload_section_appends_and_does_not_duplicate():
    fields = {
        "service": "EKS",
        "resource_type": "eks:cluster",
        "measure": "cluster_failed_node_count",
        "condition": ">",
        "threshold": "0",
        "kind": "metric",
        "namespace": "ContainerInsights",
        "dimension": "ClusterName={name}",
        "stat": "Maximum",
        "window": "5",
        "role": "primary",
    }
    section = workload_section(fields, "")
    assert storage_has_service(section, "EKS", "eks:cluster")
    assert "EKS-S01" in section
    catalog = load_catalog(FakeConfluence(section, version=2))
    assert catalog.rules[0].resource_type == "eks:cluster"
    assert catalog.rules[0].comparison == "GreaterThanThreshold"
    again = workload_section({**fields, "symptom_id": ""}, section)
    assert "EKS-S02" in again
    assert "<h3>Symptoms</h3>" in section
    assert "Scenario IDs" in section
    assert 'ac:name="status"' in section
    paired = {
        **fields,
        "symptom_id": "",
        "conditions": [
            {"condition": ">", "threshold": "0", "kind": "metric", "namespace": "ContainerInsights"},
            {"condition": "<", "threshold": "2", "kind": "log", "namespace": "/aws/eks"},
        ],
    }
    both = workload_section(paired, "")
    assert both.count("<h2>") == 1
    catalog = load_catalog(FakeConfluence(both, version=3))
    assert [rule.rule_id for rule in catalog.rules] == ["EKS-S01", "EKS-S02"]
    assert catalog.rules[1].comparison == "LessThanThreshold"


def test_return_to_health_checker_reads_the_account_again():
    demo = Demo()
    demo.scanned_at = "2026-10-12T21:10:00+00:00"
    demo.rows = [_row("sre-dir-lab", "lambda:function", "healthy")]
    demo.message = "TABLE_NAME is sre-dir-lab. The function is healthy. KAN-82 was closed because health is healthy."

    def inventory(display=False):
        assert display is True
        demo.rows = [_row("sre-dir-lab", "lambda:function", "unhealthy", "lambda.errors_after_deploy")]
        demo.scanned_at = "2026-10-12T21:40:00+00:00"

    demo._inventory = inventory
    demo._read_catalog = lambda: None
    demo.open_health()
    page = render(demo)
    assert "2026-10-12T21:10:00+00:00" not in page
    assert "2026-10-12T21:40:00+00:00" in page
    assert "lambda:function" in page
    assert "class='health unhealthy'>unhealthy" in page
    assert "The function is healthy" not in page


def test_first_health_open_scans():
    demo = Demo()
    calls = []

    def inventory(display=False):
        calls.append(display)
        demo.rows = []
        demo.scanned_at = "2026-10-12T18:00:00+00:00"

    demo._inventory = inventory
    demo._read_catalog = lambda: None
    demo.open_health()
    demo.open_health()
    assert calls == [True, True]


def test_page_read_uses_a_display_cycle(monkeypatch):
    seen = []

    def fake_cycle(account, region, display=False):
        seen.append(display)
        raise RuntimeError("stop")

    monkeypatch.setattr("sre_agent.demo_ui.refresh_aws_login", lambda *args, **kwargs: None)
    monkeypatch.setattr("sre_agent.demo_ui.run_live_cycle", fake_cycle)
    demo = Demo()
    demo.ensure_fresh()
    assert seen == [True]


def test_verified_redrive_closes_only_after_the_queue_is_healthy(monkeypatch):
    lab = "arn:aws:sqs:us-east-2:414279120882:lab-orders"
    monkeypatch.setenv("LAB_TARGETS", lab)
    demo = Demo()
    demo.redrive_settle_attempts = 2
    demo.redrive_settle_pause = 0
    demo.issue_views = [
        {
            "key": "KAN-99",
            "summary": "Dead-letter queue is holding messages after transient consumer errors.",
            "proposed": "Run sqs.redrive_dlq. Move messages from the dead-letter queue back to the source queue.",
            "resource_name": "lab-orders",
            "resource_id": lab,
            "resource_type": "sqs:queue",
        }
    ]
    depths = iter([(2, 1), (0, 0)])
    reads = []

    def read_depths(resource_id):
        value = next(depths)
        reads.append((resource_id, value))
        return value

    closed = []
    demo._run_lab_redrive = lambda resource_id, name: "sqs.redrive_dlq ran on the lab target. Moved 1."
    demo._read_queue_depths = read_depths
    demo._inventory = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full inventory"))
    demo._close_issue = lambda key, phrase="because health is healthy": closed.append((key, phrase)) or f"{key} was closed {phrase}."
    monkeypatch.setattr("sre_agent.demo_ui.time.sleep", lambda _seconds: None)
    demo.take_issue("KAN-99")
    assert reads == [(lab, (2, 1)), (lab, (0, 0))]
    assert closed == [("KAN-99", "because health is healthy")]
    assert "KAN-99 was closed" in demo.ticket_notes["KAN-99"]
    assert "Mail was sent" not in demo.message


def test_verified_redrive_stays_open_until_health_is_healthy(monkeypatch):
    lab = "arn:aws:sqs:us-east-2:414279120882:lab-orders"
    monkeypatch.setenv("LAB_TARGETS", lab)
    demo = Demo()
    demo.redrive_settle_attempts = 2
    demo.redrive_settle_pause = 0
    demo.issue_views = [
        {
            "key": "KAN-99",
            "summary": "Dead-letter queue is holding messages.",
            "proposed": "Run sqs.redrive_dlq.",
            "resource_name": "lab-orders",
            "resource_id": lab,
            "resource_type": "sqs:queue",
        }
    ]
    reads = []
    demo._read_queue_depths = lambda resource_id: reads.append(resource_id) or (4, 2)
    closed = []
    demo._run_lab_redrive = lambda resource_id, name: "sqs.redrive_dlq ran on the lab target. Moved 1."
    demo._inventory = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full inventory"))
    demo._close_issue = lambda key, phrase="because health is healthy": closed.append(key) or ""
    monkeypatch.setattr("sre_agent.demo_ui.time.sleep", lambda _seconds: None)
    demo.take_issue("KAN-99")
    assert reads == [lab, lab]
    assert closed == []
    assert "stays open" in demo.ticket_notes["KAN-99"]
    assert "Visible messages are 4" in demo.ticket_notes["KAN-99"]
    assert "Mail was sent" not in demo.message
    assert "Mail was not sent because the ticket stayed open." not in demo.message


def test_redrive_that_moves_nothing_does_not_close(monkeypatch):
    lab = "arn:aws:sqs:us-east-2:414279120882:lab-orders"
    monkeypatch.setenv("LAB_TARGETS", lab)
    demo = Demo()
    demo.issue_views = [
        {
            "key": "KAN-99",
            "summary": "Dead-letter queue is holding messages.",
            "proposed": "Run sqs.redrive_dlq.",
            "resource_name": "lab-orders",
            "resource_id": lab,
            "resource_type": "sqs:queue",
        }
    ]
    demo.rows = [
        {
            "name": "lab-orders",
            "resource_id": lab,
            "resource_type": "sqs:queue",
            "health": "healthy",
            "problem_code": "healthy",
        }
    ]
    closed = []
    demo._run_lab_redrive = lambda resource_id, name: "sqs.redrive_dlq did not move messages. No verified transient orders to replay."
    demo._inventory = lambda **_kwargs: None
    demo._close_issue = lambda key, phrase="because health is healthy": closed.append(key) or ""
    demo.take_issue("KAN-99")
    assert closed == []
    assert "stays open" in demo.ticket_notes["KAN-99"]
    assert demo.mail_note == "Mail was not sent because the verified action was not applied."
    assert "Mail was sent" not in demo.message


def test_directory_restore_closes_when_the_env_is_the_real_table():
    demo = Demo()
    demo.issue_views = [{
        "key": "KAN-7",
        "summary": "The directory function points at the wrong table.",
        "proposed": "Set Lambda function sre-dir-lab TABLE_NAME to sre-dir-lab.",
        "resource_name": "sre-dir-lab",
        "resource_id": "arn:aws:lambda:us-east-2:1:function:sre-dir-lab",
        "resource_type": "lambda:function",
    }]
    updates = []
    invoked = []
    demo._set_directory_table = lambda table_name, function_name="": updates.append((function_name, table_name))
    demo._read_function_table = lambda function_name: "missing-table" if not updates else "sre-dir-lab"
    demo._invoke_function = lambda function_name: invoked.append(function_name) or ""
    demo._set_lab_alarm_ok = lambda function_name: "CloudWatch alarm sre-dir-lab-lambda-errors is OK."
    demo._inventory = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full inventory"))
    demo._wait = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("cloudwatch wait"))
    closed = []
    demo._close_issue = lambda key, phrase="because health is healthy": closed.append((key, phrase)) or f"{key} was closed {phrase}."
    demo.take_issue("KAN-7")
    assert updates == [("sre-dir-lab", "sre-dir-lab")]
    assert invoked == ["sre-dir-lab"]
    assert "TABLE_NAME was missing-table" in demo.ticket_notes["KAN-7"]
    assert closed == [("KAN-7", "because health is healthy")]
    assert demo.directory_changed is True
    assert "TABLE_NAME is sre-dir-lab" in demo.ticket_notes["KAN-7"]
    assert "KAN-7 was closed" in demo.message
    assert "Mail was sent" not in demo.message

    missed = Demo()
    missed.issue_views = demo.issue_views
    missed._set_directory_table = lambda table_name, function_name="": None
    missed._read_function_table = lambda function_name: "missing-table"
    missed._inventory = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full inventory"))
    missed._close_issue = lambda key, phrase="because health is healthy": (_ for _ in ()).throw(AssertionError("closed"))
    missed.take_issue("KAN-7")
    assert "stays open" in missed.ticket_notes["KAN-7"]
    assert "Mail was not sent because the ticket stayed open." not in missed.message
    assert "Mail was sent" not in missed.message


def test_directory_restore_sends_mail_after_the_aws_update(monkeypatch, tmp_path):
    import boto3

    from sre_agent import demo_mail

    monkeypatch.setenv("SRE_TEST_MAIL", "1")
    monkeypatch.setenv("SES_FROM_ADDRESS", "sender@example.com")
    monkeypatch.setenv("SRE_REGION", "us-east-2")
    monkeypatch.setattr("sre_agent.local_config.load_local_config", lambda root=None: None)
    monkeypatch.setattr(demo_mail, "RECIPIENTS_PATH", tmp_path / "recipients.local.json")
    demo_mail.save_recipients("reader@example.com")
    calls = []

    class Client:
        def send_email(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "accepted"}

        def set_alarm_state(self, **kwargs):
            return {}

        def describe_alarms(self, **kwargs):
            return {"MetricAlarms": [{"StateValue": "OK"}]}

        def get_metric_statistics(self, **kwargs):
            return {"Datapoints": [{"Timestamp": "2026-10-13T00:00:00Z", "Sum": 0}]}

    class _Session:
        def __init__(self, region_name=None):
            pass

        def client(self, service, region_name=None):
            return Client()

    monkeypatch.setattr(boto3, "Session", _Session)
    demo = Demo()
    demo.issue_views = [{
        "key": "KAN-7",
        "summary": "The directory function points at the wrong table.",
        "proposed": "Set Lambda function sre-dir-lab TABLE_NAME to sre-dir-lab.",
        "resource_name": "sre-dir-lab",
        "resource_id": "arn:aws:lambda:us-east-2:1:function:sre-dir-lab",
        "resource_type": "lambda:function",
    }]
    demo._set_directory_table = lambda table_name, function_name="": None
    demo._read_function_table = lambda function_name: "sre-dir-lab"
    demo._invoke_function = lambda function_name: ""
    demo._set_lab_alarm_ok = lambda function_name: "CloudWatch alarm sre-dir-lab-lambda-errors is OK."
    demo._close_issue = lambda key, phrase="because health is healthy": f"{key} was closed {phrase}."
    demo.take_issue("KAN-7")
    assert len(calls) == 1
    assert calls[0]["FromEmailAddress"] == "ordersre840@gmail.com"
    assert calls[0]["Destination"]["ToAddresses"] == ["ordersre840@gmail.com"]
    body = calls[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert "What happened" in body
    assert "Root cause" in body
    assert "What we did" in body
    assert "Delta" in body
    assert "TABLE_NAME: sre-dir-lab -> sre-dir-lab" in body
    assert "ALARM -> OK" in body
    assert demo.mail_note == "Mail was sent to ordersre840@gmail.com."


def test_open_ticket_still_reports_the_mail_result(monkeypatch, tmp_path):
    import boto3

    from sre_agent import demo_mail

    lab = "arn:aws:sqs:us-east-2:414279120882:lab-orders"
    monkeypatch.setenv("LAB_TARGETS", lab)
    monkeypatch.setenv("SRE_TEST_MAIL", "1")
    monkeypatch.setenv("SES_FROM_ADDRESS", "sender@example.com")
    monkeypatch.setenv("SRE_REGION", "us-east-2")
    monkeypatch.setattr("sre_agent.local_config.load_local_config", lambda root=None: None)
    monkeypatch.setattr(demo_mail, "RECIPIENTS_PATH", tmp_path / "recipients.local.json")
    demo_mail.save_recipients("reader@example.com")
    calls = []
    state = {"response": {"MessageId": "accepted"}}

    class Client:
        def send_email(self, **kwargs):
            calls.append(kwargs["Destination"]["ToAddresses"])
            return state["response"]

    class _Session:
        def __init__(self, region_name=None):
            pass

        def client(self, service, region_name=None):
            return Client()

    monkeypatch.setattr(boto3, "Session", _Session)
    demo = Demo()
    demo.redrive_settle_attempts = 1
    demo.redrive_settle_pause = 0
    demo.issue_views = [
        {
            "key": "KAN-99",
            "summary": "Dead-letter queue is holding messages.",
            "proposed": "Run sqs.redrive_dlq.",
            "resource_name": "lab-orders",
            "resource_id": lab,
            "resource_type": "sqs:queue",
        }
    ]
    demo._run_lab_redrive = lambda resource_id, name: "sqs.redrive_dlq ran on the lab target. Moved 1."
    demo._read_queue_depths = lambda resource_id: (4, 2)
    demo._inventory = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full inventory"))
    demo._close_issue = lambda key, phrase="because health is healthy": (_ for _ in ()).throw(AssertionError("closed"))
    monkeypatch.setattr("sre_agent.demo_ui.time.sleep", lambda _seconds: None)
    demo.take_issue("KAN-99")
    assert "stays open" in demo.message
    assert calls == [["reader@example.com"]]
    assert demo.mail_note == "Mail was sent to the saved addresses."
    assert "sender@example.com" not in demo.message

    state["response"] = {}
    calls.clear()
    demo.take_issue("KAN-99")
    assert calls == [["reader@example.com"]]
    assert "Mail was sent" not in demo.message
    assert "did not accept" in demo.mail_note
    assert "stays open" in demo.message

    calls.clear()

    monkeypatch.delenv("SRE_TEST_MAIL", raising=False)
    monkeypatch.setattr(boto3, "Session", lambda region_name=None: (_ for _ in ()).throw(AssertionError("SES was called")))
    quiet = Demo()
    quiet._finish_verified("KAN-99", "The ticket stays open because the queue is not empty.", False, applied=True)
    assert calls == []
    assert "Mail was sent" not in quiet.message


def test_display_cycle_does_not_call_the_narrator_or_jira():
    from sre_agent.agents import narrator
    from sre_agent.lab.scenarios import build_world
    from sre_agent.models import AgentConfig
    from sre_agent.registry.store import Registry
    from sre_agent.service import OperationsAgent

    class Jira:
        def __init__(self) -> None:
            self.calls = 0

        def sync(self, diagnosis) -> str:
            self.calls += 1
            return "KAN-1"

    world = build_world()
    jira = Jira()
    agent = OperationsAgent(
        world.scope,
        Registry(),
        world.clock,
        AgentConfig(mode="recommend", require_approval=True),
        jira=jira,
    )
    seen = []
    original = narrator.explain

    def spy(*args, **kwargs):
        seen.append("explain")
        return original(*args, **kwargs)

    narrator.explain = spy
    try:
        report = agent.run_display_cycle()
    finally:
        narrator.explain = original
    assert seen == []
    assert jira.calls == 0
    assert report.narrations == []
    assert report.actions == []
    assert report.verifications == []
    assert any(item.presence == "present" for item in report.resources)
    assert any(item.state in {"healthy", "degraded", "unhealthy"} for item in report.assessments)


def test_catalog_skip_notes_are_absent_from_the_page():
    demo = Demo()
    demo.scan_notes = [
        "Catalog rule SQS-S05 was not evaluated because errors could not be read "
        "(ResourceNotFoundException: The specified log group does not exist: /aws/lambda/sre-agent-repair).",
        "Catalog rule SQS-S04 was not evaluated because ${dlq} has no value.",
        "Catalog rule APIGW-S02 was not evaluated because ${apigw_access_log_group} has no value. "
        "API-Gateway-Execution-Logs_vdq2aibp54/prod",
        "confluence (*) error: Catalog load failed. Built-in checks still ran. HTTP Error 404: Not Found",
        "confluence load failed for a different reason.",
    ]
    page = render(demo)
    assert "was not evaluated" not in page
    assert "ResourceNotFoundException" not in page
    assert "${dlq}" not in page
    assert "${apigw_access_log_group}" not in page
    assert "SQS-S05" not in page
    assert "Catalog load failed" not in page
    assert "HTTP Error 404" not in page
    assert "confluence load failed for a different reason." in page
    assert "was not evaluated" in " ".join(demo.scan_notes)


def _symptom_table(heading: str, rows: list[tuple]) -> str:
    header = (
        "<tr><th>Symptom ID</th><th>Metric / query</th><th>Condition</th><th>Threshold</th>"
        "<th>Status</th><th>Type</th><th>Role</th></tr>"
    )
    body = ""
    for symptom, measure, condition, threshold, role in rows:
        body += (
            "<tr>"
            f"<td>{symptom}</td><td>{measure}</td><td>{condition}</td><td>{threshold}</td>"
            f"<td>active</td><td>metric</td><td>{role}</td>"
            "</tr>"
        )
    return f"<h2>{heading}</h2><table>{header}{body}</table>"


def test_confluence_section_is_read_only():
    from sre_agent.integrations.confluence import catalog_sections

    storage = """
    <h2>Service: SQS (sqs:queue)</h2>
    <table>
    <tr><th>Symptom ID</th><th>Metric / query</th><th>Condition</th><th>Threshold</th><th>Status</th><th>Type</th><th>Role</th></tr>
    <tr><td>SQS-S04</td><td>ApproximateNumberOfMessagesVisible</td><td>&gt;</td><td>0</td><td>active</td><td>metric</td><td>primary</td></tr>
    <tr><td>SQS-S05</td><td>${dlq}</td><td>&gt;</td><td></td><td>active</td><td>log</td><td>primary</td></tr>
    </table>
    """
    storage += _symptom_table(
        "Service: Lambda (lambda:function)",
        [("LAMBDA-S01", "Errors", ">", "0", "primary")],
    )
    storage += _symptom_table(
        "Service: DynamoDB (dynamodb:table)",
        [("DDB-S01", "ConsumedReadCapacityUnits", ">", "80", "supporting")],
    )
    storage += _symptom_table(
        "Service: API Gateway (apigateway:restapi)",
        [("APIGW-S01", "5XXError", ">", "0", "primary")],
    )
    storage += _symptom_table(
        "Service: SNS (sns:topic)",
        [("SNS-S01", "NumberOfNotificationsFailed", ">", "0", "primary")],
    )
    demo = Demo()
    demo.catalog_sections = catalog_sections(storage)
    page = render(demo)
    assert page.count("<details class='catalog-card'>") == 5
    for name in ("SQS", "Lambda", "DynamoDB", "API Gateway", "SNS"):
        assert f"<span class='catalog-name'>{name}</span>" in page
    assert "<span class='catalog-count'>1 metric</span>" in page
    assert "Service: SQS (sqs:queue)" not in page
    assert "<th>Metric</th><th>Why</th><th>Symptom ID</th>" in page
    assert "ApproximateNumberOfMessagesVisible" in page
    assert "Unhealthy when ApproximateNumberOfMessagesVisible is above 0." in page
    assert "SQS-S04" in page
    assert "Supporting signal. Degraded when ConsumedReadCapacityUnits is above 80." in page
    assert "Unhealthy when Errors is above 0." in page
    assert "Unhealthy when 5XXError is above 0." in page
    assert "Unhealthy when NumberOfNotificationsFailed is above 0." in page
    assert "${dlq}" not in page
    assert "SQS-S05" not in page
    assert "ResourceNotFoundException" not in page
    assert "Populate" not in page
    assert 'id="workload-form"' not in page
    assert "Add a service" not in page
    assert "Service name" not in page


def test_where_seen_lab_target_is_runnable_and_a_live_queue_is_not():
    lab = "arn:aws:sqs:us-east-2:414279120882:sre-fault-lab-jobs"
    live = "arn:aws:sqs:us-east-2:414279120882:sre-agent-work"
    allowed = ticket_decision(
        "Run sqs.redrive_dlq. Move messages from the dead-letter queue back to the source queue.",
        where_seen=lab + "|sre-fault-lab-jobs",
        lab_targets={lab},
    )
    assert allowed["run"] is True
    assert allowed["target"] == lab
    refused = ticket_decision(
        "Run sqs.redrive_dlq.",
        resource_name="sre-agent-work",
        resource_id=live,
        where_seen=live,
        lab_targets={lab, live},
    )
    assert refused["run"] is False


def test_verify_apply_runs_only_the_allowed_ticket_action(monkeypatch):
    import json
    import threading
    from urllib.request import Request, urlopen

    from sre_agent.demo_ui import DemoServer, make_handler

    lab = "arn:aws:sqs:us-east-2:414279120882:sre-fault-lab-jobs"
    monkeypatch.setenv("LAB_TARGETS", lab)
    demo = Demo()
    demo.issue_views = [
        {
            "key": "KAN-67",
            "summary": "Dead-letter queue is holding messages after transient consumer errors.",
            "proposed": "Run sqs.redrive_dlq. Move messages from the dead-letter queue back to the source queue.",
            "where_seen": lab + "|sre-fault-lab-jobs",
            "resource_name": "sre-fault-lab-jobs",
            "resource_id": lab,
            "resource_type": "sqs:queue",
        }
    ]
    calls = []
    demo._run_lab_redrive = lambda resource_id, name: calls.append(("redrive", resource_id)) or "sqs.redrive_dlq ran on the lab target. Moved 1."
    demo._inventory = lambda **_kwargs: demo.rows.append(
        {"name": "sre-fault-lab-jobs", "resource_id": lab, "resource_type": "sqs:queue", "health": "healthy", "problem_code": "healthy"}
    )
    demo._close_issue = lambda key, phrase="because health is healthy": calls.append(("close", key, phrase)) or f"{key} was closed {phrase}."
    server = DemoServer(("127.0.0.1", 0), make_handler(demo))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def post(path, payload):
        data = json.dumps(payload).encode()
        request = Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())

    try:
        status, plan = post("/api/plan", {"action": "ticket", "issue": "KAN-67", "proposed": demo.issue_views[0]["proposed"]})
        assert status == 200
        assert plan["applied"] is False
        assert calls == []
        status, body = post(
            "/api/verify",
            {"action": "ticket", "issue": "KAN-67", "proposed": demo.issue_views[0]["proposed"], "verify": True},
        )
        assert status == 200
        assert body["applied"] is True
        assert ("redrive", lab) in calls
        assert ("close", "KAN-67", "because health is healthy") in calls
        calls.clear()
        demo.rows.clear()
        demo.issue_views[0]["proposed"] = "Delete the production table."
        status, applied = post(
            "/api/verify",
            {"action": "ticket", "issue": "KAN-67", "proposed": "Delete the production table.", "verify": True},
        )
        assert status == 200
        assert applied["applied"] is True
        assert ("redrive", lab) in calls
        assert ("close", "KAN-67", "because health is healthy") in calls
        assert "not run" not in applied["message"].lower()
        assert "Delete the production table" not in applied["message"]
    finally:
        server.shutdown()


def test_mail_save_does_not_require_verification(tmp_path, monkeypatch):
    import boto3

    from sre_agent import demo_mail

    monkeypatch.setattr(demo_mail, "RECIPIENTS_PATH", tmp_path / "recipients.local.json")
    saved = demo_mail.save_recipients("reader@example.com")
    assert saved["ok"] is True
    assert saved["addresses"] == ["reader@example.com"]
    assert "verify" not in saved
    monkeypatch.setenv("SES_FROM_ADDRESS", "sender@example.com")
    monkeypatch.setenv("SRE_REGION", "us-east-2")
    sent = []

    class Client:
        def send_email(self, **kwargs):
            sent.append(kwargs["Destination"]["ToAddresses"])
            return {"MessageId": "accepted"}

    class _Session:
        def __init__(self, region_name=None):
            pass

        def client(self, service, region_name=None):
            return Client()

    monkeypatch.setattr(boto3, "Session", _Session)
    note = demo_mail.deliver_mail("SRE agent: KAN-1 created", "A ticket was created.")
    assert note == "Mail was sent to the saved addresses."
    assert sent == [["reader@example.com"]]
    assert demo_mail.ticket_mail(object(), "KAN-1") == demo_mail.EMPTY_MAIL
    assert sent == [["reader@example.com"]]
    monkeypatch.setattr(demo_mail, "RECIPIENTS_PATH", tmp_path / "missing.json")

    class _Boom:
        def __init__(self, region_name=None):
            pass

        def client(self, service, region_name=None):
            raise AssertionError("SES was called")

    monkeypatch.setattr(boto3, "Session", _Boom)
    assert demo_mail.deliver_mail("subject", "body") == demo_mail.EMPTY_MAIL


def test_success_sentence_only_after_ses_accepts(tmp_path, monkeypatch):
    import boto3
    from botocore.exceptions import ClientError

    from sre_agent import demo_mail

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr("sre_agent.local_config.load_local_config", lambda root=None: None)
    monkeypatch.setattr(demo_mail, "RECIPIENTS_PATH", tmp_path / "recipients.local.json")
    monkeypatch.setenv("SRE_REGION", "us-east-2")
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.test")
    monkeypatch.setattr("sre_agent.integrations.jira.star_plain", lambda diagnosis: "Issue")
    calls = []
    state = {"response": None, "error": None}

    class Client:
        def send_email(self, **kwargs):
            calls.append(kwargs["Destination"]["ToAddresses"])
            if state["error"] is not None:
                raise state["error"]
            return state["response"]

    class _Session:
        def __init__(self, region_name=None):
            pass

        def client(self, service, region_name=None):
            return Client()

    monkeypatch.setattr(boto3, "Session", _Session)
    skipped = demo_mail.deliver_mail("subject", "body")
    assert "Mail was sent" not in skipped
    assert skipped == demo_mail.EMPTY_MAIL
    assert calls == []
    skipped_ticket = demo_mail.ticket_mail(object(), "KAN-1")
    assert "Mail was sent" not in skipped_ticket
    assert calls == []

    demo_mail.save_recipients("reader@example.com")
    monkeypatch.delenv("SES_FROM_ADDRESS", raising=False)
    missing = demo_mail.applied_mail("KAN-1", "Recorded the correction.")
    assert "Mail was sent" not in missing
    assert "from-address is not in the local config" in missing
    assert calls == []

    monkeypatch.setenv("SES_FROM_ADDRESS", "sender@example.com")
    state["error"] = ClientError(
        {
            "Error": {
                "Code": "MessageRejected",
                "Message": "Email address is not verified. The following identities failed the check in region US-EAST-2: reader@example.com. The account is in the sandbox.",
            }
        },
        "SendEmail",
    )
    rejected = demo_mail.deliver_mail("SRE agent: KAN-1 created", "A ticket was created.")
    assert "Mail was sent" not in rejected
    assert rejected == "Mail was not sent because SES rejected an unverified recipient while the account is in the sandbox."
    assert "sender@example.com" not in rejected
    assert calls == [["reader@example.com"]]

    state["error"] = ClientError(
        {
            "Error": {
                "Code": "MessageRejected",
                "Message": "Email address is not verified. The following identities failed the check in region US-EAST-2: reader@example.com",
            }
        },
        "SendEmail",
    )
    plain = demo_mail.deliver_mail("SRE agent: KAN-1 created", "A ticket was created.")
    assert "Mail was sent" not in plain
    assert plain == "Mail was not sent because SES rejected an unverified recipient while the account is in the sandbox."
    assert "reader@example.com" not in plain
    assert "sender@example.com" not in plain

    state["error"] = None
    state["response"] = {}
    unaccepted = demo_mail.applied_mail("KAN-1", "Recorded the correction.")
    assert "Mail was sent" not in unaccepted
    assert "did not accept" in unaccepted

    state["response"] = {"MessageId": "accepted"}
    sent = demo_mail.ticket_mail(object(), "KAN-1")
    assert sent == "Mail was sent to the saved addresses."
    verified = demo_mail.applied_mail("KAN-1", "Recorded the correction.")
    assert verified == "Mail was sent to the saved addresses."
    assert calls == [["reader@example.com"]] * 5


def test_saved_address_dropdown_and_delete(tmp_path, monkeypatch):
    from sre_agent import demo_mail
    from sre_agent.demo_ui import Demo, render

    monkeypatch.setattr(demo_mail, "RECIPIENTS_PATH", tmp_path / "recipients.local.json")
    empty = render(Demo())
    assert '<select id="saved-addresses">' in empty
    assert "No addresses are saved" in empty
    assert 'id="delete-email"' in empty
    assert 'id="verify-email"' in empty
    assert "<textarea" not in empty

    demo_mail.add_recipient("one@example.com")
    demo_mail.add_recipient("two@example.com")
    listed = render(Demo())
    assert '<option value="one@example.com">one@example.com</option>' in listed
    assert '<option value="two@example.com">two@example.com</option>' in listed
    removed = demo_mail.delete_recipient("one@example.com")
    assert removed["ok"] is True
    assert removed["addresses"] == ["two@example.com"]
    after = render(Demo())
    assert "one@example.com" not in after
    assert '<option value="two@example.com">two@example.com</option>' in after
    assert demo_mail.load_recipients() == ["two@example.com"]
    page = render(Demo())
    assert 'id="mail-verify-overlay"' in page
    assert "Send verification email" in page
    assert "Check verification" in page


def test_verify_address_asks_ses(monkeypatch):
    from sre_agent import demo_mail

    calls = []

    class Client:
        def verify_email_identity(self, EmailAddress):
            calls.append(("verify", EmailAddress))

        def get_identity_verification_attributes(self, Identities):
            calls.append(("status", Identities[0]))
            return {"VerificationAttributes": {Identities[0]: {"VerificationStatus": "Success"}}}

    monkeypatch.setenv("SRE_TEST_MAIL", "1")
    monkeypatch.setenv("SRE_REGION", "us-east-2")
    import boto3

    class _Session:
        def __init__(self, region_name=None):
            pass

        def client(self, service, region_name=None):
            return Client()

    monkeypatch.setattr(boto3, "Session", _Session)
    started = demo_mail.request_verification("reader@example.com")
    assert started["ok"] is True
    assert started["status"] == "Pending"
    assert "reader@example.com" in started["message"]
    checked = demo_mail.verification_status("reader@example.com")
    assert checked["status"] == "Success"
    assert "is verified" in checked["message"]
    assert calls == [("verify", "reader@example.com"), ("status", "reader@example.com")]
    already = demo_mail.confirm_verification("reader@example.com")
    assert already["status"] == "Success"
    assert calls == [("verify", "reader@example.com"), ("status", "reader@example.com"), ("status", "reader@example.com")]


def test_verify_apply_calls_aws_and_closes_when_healthy(monkeypatch, tmp_path):
    import json
    import threading
    from types import SimpleNamespace
    from urllib.request import Request, urlopen

    import boto3

    from sre_agent import demo_mail
    from sre_agent.demo_ui import DemoServer, make_handler

    lab = "arn:aws:sqs:us-east-2:414279120882:tiny-workload-orders"
    dlq = "arn:aws:sqs:us-east-2:414279120882:tiny-workload-orders-dlq"
    monkeypatch.setenv("LAB_TARGETS", lab)
    calls = []

    class Reader:
        def start_message_move_task(self, account_id, region, source_arn, dlq_arn):
            calls.append(("start_message_move_task", source_arn, dlq_arn))
            return {"task_id": "task-1", "status": "RUNNING"}

    class Tools:
        def inspect(self, resource_id, incident_id):
            return None

        def snapshot(self):
            return SimpleNamespace(logs={}, dlq_messages=[])

    class Registry:
        def get_by_resource_id(self, resource_id):
            return SimpleNamespace(
                resource_id=lab,
                name="tiny-workload-orders",
                account_id="414279120882",
                region="us-east-2",
                config={"redrive_policy": {"deadLetterTargetArn": dlq}},
            )

        def list_resources(self):
            return []

    demo = Demo()
    demo.agent = SimpleNamespace(registry=Registry(), tools=Tools(), scope=SimpleNamespace())
    demo.redrive_settle_attempts = 1
    demo.redrive_settle_pause = 0
    demo.issue_views = [{
        "key": "KAN-80",
        "summary": "Dead-letter queue is holding messages.",
        "proposed": "Run sqs.redrive_dlq.",
        "resource_name": "tiny-workload-orders",
        "resource_id": lab,
        "resource_type": "sqs:queue",
        "where_seen": lab,
    }]

    inventories = []
    demo._inventory = lambda **_kwargs: inventories.append("inventory")
    demo._read_queue_depths = lambda resource_id: (0, 0)
    demo._close_issue = (
        lambda key, phrase="because health is healthy": calls.append(("close", key, phrase)) or f"{key} was closed {phrase}."
    )
    monkeypatch.setattr(demo_mail, "RECIPIENTS_PATH", tmp_path / "recipients.local.json")
    demo_mail.save_recipients("reader@example.com")
    monkeypatch.setenv("SRE_TEST_MAIL", "1")
    monkeypatch.setenv("SES_FROM_ADDRESS", "sender@example.com")
    monkeypatch.setenv("SRE_REGION", "us-east-2")
    monkeypatch.setattr("sre_agent.local_config.load_local_config", lambda root=None: None)
    ses = {"response": {}, "calls": []}

    class Ses:
        def send_email(self, **kwargs):
            ses["calls"].append(kwargs["Destination"]["ToAddresses"])
            return ses["response"]

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: Ses())
    monkeypatch.setattr("sre_agent.discovery.service._reader_for", lambda scope, account: Reader())
    monkeypatch.setattr("sre_agent.demo_ui.time.sleep", lambda _seconds: None)
    server = DemoServer(("127.0.0.1", 0), make_handler(demo))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def post(path, payload):
        data = json.dumps(payload).encode()
        request = Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())

    try:
        status, body = post(
            "/api/verify",
            {"action": "ticket", "issue": "KAN-80", "proposed": "Run sqs.redrive_dlq.", "verify": True},
        )
        assert status == 200
        assert body["applied"] is True
        assert inventories == []
        assert ("start_message_move_task", lab, dlq) in calls
        assert ("close", "KAN-80", "because health is healthy") in calls
        assert ses["calls"] == [["reader@example.com"]]
        assert "Mail was sent" not in (body.get("mail") or "")
        assert "Mail was sent" not in (body.get("message") or "")
        assert "did not accept" in (body.get("mail") or "")
        assert "sender@example.com" not in json.dumps(body)

        ses["response"] = {"MessageId": "accepted"}
        status, body = post(
            "/api/verify",
            {"action": "ticket", "issue": "KAN-80", "proposed": "Run sqs.redrive_dlq.", "verify": True},
        )
        assert status == 200
        assert body["applied"] is True
        assert ses["calls"] == [["reader@example.com"], ["reader@example.com"]]
        assert body["mail"] == "Mail was sent to the saved addresses."
        assert "Mail was sent to the saved addresses." in body["message"]
        assert "sender@example.com" not in json.dumps(body)
    finally:
        server.shutdown()


def test_checker_queue_has_no_noop_verify_and_mail_stays_outside_pytest(monkeypatch, tmp_path):
    import json
    import threading
    from urllib.request import Request, urlopen

    from sre_agent import demo_mail
    from sre_agent.demo_ui import DemoServer, make_handler, render_jira
    from sre_agent.integrations.jira import FakeJira, JiraIssue, proposed_solution
    from sre_agent.models import Diagnosis

    placeholder = (
        "Name the configuration or metric change that corrects this problem. "
        "Nothing runs until you verify that change."
    )
    queue = "https://sqs.us-east-2.amazonaws.com/414279120882/sre-agent-work"
    demo = Demo()
    demo.rows = [{
        "name": "sre-agent-work",
        "resource_id": queue,
        "resource_type": "sqs:queue",
        "health": "unhealthy",
        "problem_code": "sqs.no_consumer",
        "problem": "Queue backlog and no consumer relationship is recorded.",
        "discovery_source": "api",
        "presence": "present",
    }]
    demo.issue_views = [{
        "key": "KAN-69",
        "summary": "Queue backlog and no consumer relationship is recorded.",
        "status": "To Do",
        "situation": "Queue backlog and no consumer relationship is recorded.",
        "where_seen": queue + "|sre-agent-work",
        "proposed": placeholder,
        "url": "https://example.test/browse/KAN-69",
        "resource_name": "sre-agent-work",
        "resource_id": queue,
        "resource_type": "sqs:queue",
    }]
    page = render_jira(demo)
    assert placeholder not in page
    assert "Take this action" not in page
    assert "Populate" not in page
    assert "queue was not changed" not in page.lower()
    dlq = resolved_proposal(
        {
            "proposed": placeholder,
            "resource_name": "sre-agent-work-dlq",
            "situation": "This dead-letter queue has messages.",
        }
    )
    assert dlq == ""
    assert "sqs.redrive_dlq" not in dlq
    solution = proposed_solution(
        Diagnosis(
            incident_id="inc-69",
            resource_id=queue,
            observed_problem="Queue backlog and no consumer relationship is recorded.",
            problem_code="sqs.no_consumer",
            health="unhealthy",
        )
    )
    assert placeholder not in solution
    assert solution == ""
    assert concrete_correction({"resource_name": "sre-agent-work"}) == ""

    jira = FakeJira()
    jira.issues["KAN-69"] = JiraIssue(key="KAN-69", dedup_key="problem|sqs.no_consumer", summary="backlog")
    demo.jira_client = jira
    demo._inventory = lambda **_kwargs: None
    redriven = []
    demo._run_lab_redrive = lambda resource_id, name: redriven.append(resource_id)
    server = DemoServer(("127.0.0.1", 0), make_handler(demo))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def post(path, payload):
        data = json.dumps(payload).encode()
        request = Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())

    try:
        status, plan = post("/api/plan", {"action": "ticket", "issue": "KAN-69", "proposed": placeholder})
        assert status == 200
        assert plan["will_run"] is False
        assert plan["applied"] is False
        shown = " ".join(plan["items"]) + " " + plan["lead"]
        assert "was not run" not in shown.lower()
        assert "Name the configuration" not in shown
        status, body = post(
            "/api/verify",
            {"action": "ticket", "issue": "KAN-69", "proposed": placeholder, "verify": True},
        )
        assert status == 200
        assert body["applied"] is False
        assert "was not run" not in body["message"].lower()
        assert "queue was not changed" not in body["message"].lower()
        assert body.get("mail") == ""
        assert jira.transitioned == []
        assert redriven == []
        assert jira.comments.get("KAN-69", []) == []
        status, refused = post(
            "/api/verify",
            {"action": "ticket", "issue": "KAN-69", "proposed": "Run sqs.redrive_dlq.", "verify": True},
        )
        assert status == 200
        assert refused["applied"] is False
        assert redriven == []
        assert "was not run" not in refused["message"].lower()
        assert jira.transitioned == []
    finally:
        server.shutdown()

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(demo_mail, "RECIPIENTS_PATH", tmp_path / "recipients.local.json")
    demo_mail.save_recipients("reader@example.com")
    sent = []

    def capture(subject, body):
        sent.append(subject)
        return "Mail was sent to the saved addresses."

    monkeypatch.setattr(demo_mail, "deliver_mail", capture)
    assert demo_mail.applied_mail("KAN-69", "Recorded the correction.") == "Mail was sent to the saved addresses."
    assert sent == ["SRE agent: KAN-69 action applied"]


def test_missing_directory_does_not_raise_the_waiter_into_the_page(monkeypatch):
    from sre_agent.demo_ui import DIRECTORY_ABSENT

    calls = []

    class Missing(Exception):
        pass

    class Client:
        class exceptions:
            ResourceNotFoundException = Missing
            ResourceConflictException = type("ResourceConflictException", (Exception,), {})

        def get_function(self, FunctionName):
            calls.append(("get", FunctionName))
            raise self.exceptions.ResourceNotFoundException(
                "An error occurred (ResourceNotFoundException) when calling the GetFunction operation: "
                "Function not found: arn:aws:lambda:us-east-2:414279120882:function:enterprise-sites-directory"
            )

        def get_waiter(self, name):
            calls.append(("waiter", name))
            raise AssertionError("waiter should not run")

        def update_function_configuration(self, **kwargs):
            calls.append(("update", kwargs.get("FunctionName")))
            raise AssertionError("update should not run")

    class Session:
        def client(self, service, region_name=None):
            assert service == "lambda"
            return Client()

    monkeypatch.setattr("sre_agent.demo_ui.refresh_aws_login", lambda *args, **kwargs: None)
    import boto3

    monkeypatch.setattr(boto3, "Session", lambda region_name=None: Session())
    demo = Demo()
    try:
        demo._set_directory_table("enterprise-sites")
    except RuntimeError as exc:
        assert str(exc) == DIRECTORY_ABSENT
    else:
        raise AssertionError("a missing directory function should be a plain message")
    assert calls == [("get", "enterprise-sites-directory")]
    demo.fail(
        RuntimeError(
            "Waiter FunctionUpdated failed: An error occurred (ResourceNotFoundException): "
            "Function not found: arn:aws:lambda:us-east-2:414279120882:function:enterprise-sites-directory"
        )
    )
    assert demo.error == DIRECTORY_ABSENT
    assert "FunctionUpdated" not in demo.error
    assert "ResourceNotFoundException" not in demo.error
    page = render(demo)
    assert "FunctionUpdated" not in page
    assert "ResourceNotFoundException" not in page
    assert "Waiter FunctionUpdated" not in page

    demo.error = (
        "Waiter FunctionUpdated failed: An error occurred (ResourceNotFoundException): "
        "Function not found: arn:aws:lambda:us-east-2:414279120882:function:enterprise-sites-directory"
    )
    demo.message = "The last action failed."
    demo.phase = "The last action failed. " + demo.error
    demo.issue_views = []
    jira = render_jira(demo)
    assert "last action failed" not in jira.lower()
    assert "FunctionUpdated" not in jira
    assert "ResourceNotFoundException" not in jira
    assert "enterprise-sites-directory" not in jira
    assert "No issues are on the board." in jira
    assert demo.error == ""

    updates = []
    demo._set_directory_table = lambda table_name: updates.append(table_name)
    demo._inventory = lambda display=False: None
    demo.ensure_fresh()
    assert updates == []


def test_directory_restore_still_waits_when_the_function_exists(monkeypatch):
    calls = []

    class Waiter:
        def wait(self, FunctionName, WaiterConfig):
            calls.append(("wait", FunctionName))

    class Client:
        class exceptions:
            ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
            ResourceConflictException = type("ResourceConflictException", (Exception,), {})

        def get_function(self, FunctionName):
            calls.append(("get", FunctionName))
            return {"Configuration": {"FunctionName": FunctionName}}

        def get_waiter(self, name):
            calls.append(("waiter", name))
            return Waiter()

        def update_function_configuration(self, **kwargs):
            calls.append(("update", kwargs["Environment"]["Variables"]["TABLE_NAME"]))

    class Session:
        def client(self, service, region_name=None):
            return Client()

    monkeypatch.setattr("sre_agent.demo_ui.refresh_aws_login", lambda *args, **kwargs: None)
    import boto3

    monkeypatch.setattr(boto3, "Session", lambda region_name=None: Session())
    demo = Demo()
    demo._set_directory_table("enterprise-sites")
    assert ("get", "enterprise-sites-directory") in calls
    assert ("waiter", "function_updated") in calls
    assert calls.count(("wait", "enterprise-sites-directory")) == 2
    assert ("update", "enterprise-sites") in calls


def test_login_export_does_not_reuse_the_copied_token(monkeypatch):
    import json

    import sre_agent.demo_ui as demo_ui

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAOLD")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "old-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "old-token")
    seen = {}

    def fake_run(command, capture_output, text, check, env=None):
        seen["env"] = env

        class Result:
            returncode = 0
            stdout = json.dumps(
                {
                    "AccessKeyId": "ASIANEW",
                    "SecretAccessKey": "new-secret",
                    "SessionToken": "new-token",
                    "Expiration": "2099-01-01T00:00:00+00:00",
                }
            )
            stderr = ""

        return Result()

    monkeypatch.setattr(demo_ui.subprocess, "run", fake_run)
    previous_deadline = demo_ui._login_deadline
    demo_ui._login_deadline = 0.0
    try:
        demo_ui.refresh_aws_login(force=True)
    finally:
        demo_ui._login_deadline = previous_deadline
    assert "AWS_ACCESS_KEY_ID" not in seen["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in seen["env"]
    assert "AWS_SESSION_TOKEN" not in seen["env"]


def test_expired_login_starts_sign_in_and_retries(monkeypatch):
    import json
    import os

    import sre_agent.demo_ui as demo_ui

    calls = []

    def fake_run(command, capture_output=None, text=None, check=None, env=None, timeout=None):
        calls.append(list(command))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if command[-1:] == ["login"]:
            return Result()
        if sum(1 for item in calls if "export-credentials" in item) == 1:
            Result.returncode = 255
            Result.stderr = "Your session has expired."
            return Result()
        Result.stdout = json.dumps(
            {
                "AccessKeyId": "ASIANEW",
                "SecretAccessKey": "new-secret",
                "SessionToken": "new-token",
                "Expiration": "2099-01-01T00:00:00+00:00",
            }
        )
        return Result()

    monkeypatch.setattr(demo_ui.subprocess, "run", fake_run)
    previous_deadline = demo_ui._login_deadline
    demo_ui._login_deadline = 0.0
    try:
        demo_ui.refresh_aws_login(force=True)
    finally:
        demo_ui._login_deadline = previous_deadline
    assert any(item[-1:] == ["login"] for item in calls)
    assert os.environ["AWS_ACCESS_KEY_ID"] == "ASIANEW"
    assert "login credentials are not available" not in " ".join(" ".join(item) for item in calls)
