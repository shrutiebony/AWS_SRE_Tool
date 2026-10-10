import json
from pathlib import Path

from sre_agent.demo_plans import allows_apply, parse_emails, plan_for
from sre_agent.local_config import FIELDS


def test_add_plan_is_numbered_and_names_the_region():
    plan = plan_for("add", "us-east-2")
    assert plan is not None
    assert plan["requires_verify"] is True
    assert plan["button"] == "Verify and apply"
    assert plan["items"][0] == "1. Create DynamoDB table demo-extra-sites in us-east-2."
    assert plan["items"][1] == "2. Run inventory so the checker can see it."
    text = " ".join(plan["items"])
    assert "enterprise-sites" in text
    assert "sre-agent-*" in text
    assert allows_apply("add", {}) is False
    assert allows_apply("add", {"verify": True}) is True
    assert allows_apply("demo1_create", {}) is False


def test_lab_plan_is_a_simulation():
    plan = plan_for("close", "us-east-2")
    assert plan is not None
    assert plan["simulation"] is True
    assert plan["requires_verify"] is False
    assert plan["lead"].startswith("This is a simulation.")
    assert "do not run that repair on the live account" in plan["items"][1]
    assert allows_apply("demo2_lab", {}) is False
    assert allows_apply("demo2_lab", {"simulate": True}) is True
    assert allows_apply("close", {"verify": True}) is False


def test_reset_plan_keeps_protected_resources():
    plan = plan_for("reset", "us-east-2")
    assert plan is not None
    text = " ".join(plan["items"])
    assert "Delete DynamoDB table demo-extra-sites in us-east-2 if it exists." in text
    assert "Delete SNS topic demo-alerts in us-east-2 if it exists." in text
    assert "Leave DynamoDB table enterprise-sites and every sre-agent-* resource unchanged" in text


def test_example_config_has_empty_keys():
    example = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
    assert set(example) == set(FIELDS)
    assert all(value == "" for value in example.values())


def test_parse_emails_accepts_commas_and_lines():
    valid, invalid = parse_emails("one@example.com, not-an-email\ntwo@example.org")
    assert valid == ["one@example.com", "two@example.org"]
    assert invalid == ["not-an-email"]
    assert parse_emails("   \n") == ([], [])
    assert parse_emails("one@example.com, one@example.com") == (["one@example.com"], [])
