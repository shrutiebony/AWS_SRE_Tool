from sre_agent.demo_plans import ticket_decision, ticket_plan
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
    main, marker, popup = page.partition('id="test-overlay"')
    assert marker
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
    assert 'id="workload-form"' in main
    assert ">Populate</button>" in main
    assert page.index('id="panel-jira"') < page.index('id="panel-confluence"')
    assert page.index('id="panel-confluence"') < page.index("<h2>Let me test how this works</h2>")
    assert page.index("<h2>Let me test how this works</h2>") < page.index("<h2>Mail</h2>")
    assert page.index("<h2>Mail</h2>") < page.index('id="test-overlay"')
    assert "Send results to these addresses" in page
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
    assert "Break the directory" not in main
    assert "Restore the directory" not in main
    assert "Directory health" not in main
    assert "Break the directory" in popup
    assert "Restore the directory" in popup
    assert "Let me test how this works" in page
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
    assert refused["message"] == "The action was not run."
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
    assert any(edited in item for item in plan["items"])
    refused = ticket_plan(
        "KAN-4",
        ticket_decision("Delete the production table."),
        "us-east-2",
        proposed="Delete the production table.",
    )
    assert refused["will_run"] is False
    assert "The edited action was not run. The ticket stays open." in " ".join(refused["items"])


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


def test_each_page_read_scans_even_when_a_scan_just_finished():
    demo = Demo()
    demo.scanned_at = "2099-01-01T00:00:00+00:00"
    calls = []

    def inventory(display=False):
        calls.append("scan")
        demo.rows = []

    demo._inventory = inventory
    demo.ensure_fresh()
    demo.ensure_fresh()
    assert calls == ["scan", "scan"]


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
