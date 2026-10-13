from sre_agent.integrations.jira import FakeJira, JiraIssue, false_alarm_text, issue_is_actionable, problem_identity, star_plain
from sre_agent.models import Diagnosis, Evidence, ProposedAction


def test_new_ticket_uses_star_sections_and_close_follows_a_verified_action():
    diagnosis = Diagnosis(
        incident_id="inc-1",
        resource_id="res-1",
        observed_problem="Lambda errors rose.",
        problem_code="lambda.errors",
        health="unhealthy",
        dedup_key="414279120882|us-east-2|res-1|lambda.errors",
        proposed_action=ProposedAction(
            runbook_id="sqs.redrive_dlq",
            target_resource_id="res-1",
            rationale="Move the transient messages back to the source queue.",
            evidence_ids=["ev-1"],
            automatic_eligible=False,
        ),
    )
    text = star_plain(diagnosis)
    assert text.startswith("* Issue\nLambda errors rose.")
    assert "\n\n* Where seen\n" in text
    assert "\n\n* Proposed solution\n" in text
    assert "sqs.redrive_dlq" in text
    assert "only after you verify it" in text

    jira = FakeJira()
    key = jira.sync(diagnosis)
    assert jira.last_created is True
    assert jira.issues[key].description == text
    diagnosis.jira_issue_key = key
    again = jira.sync(diagnosis)
    assert again == key
    assert jira.last_created is False
    jira.close(key)
    assert jira.issues[key].status == "Done"


def test_a_runbook_fault_does_not_share_a_catalog_breach_ticket():
    breach = Evidence(
        id="ev-signals",
        source="cloudwatch",
        summary="Catalog rule SQS-S04 v3: ApproximateNumberOfMessagesVisible is 1 GreaterThanThreshold 0.",
    )
    transient = Diagnosis(
        incident_id="lab",
        resource_id="arn:aws:sqs:us-east-2:414279120882:sre-fault-lab-jobs",
        observed_problem="Dead-letter queue is holding messages after transient consumer errors.",
        problem_code="sqs.dlq_transient",
        health="unhealthy",
        evidence=[breach],
        proposed_action=ProposedAction("sqs.redrive_dlq", "arn:aws:sqs:us-east-2:414279120882:sre-fault-lab-jobs", "Move the messages.", ["ev-signals"], True),
    )
    other = Diagnosis(
        incident_id="live",
        resource_id="arn:aws:sqs:us-east-2:414279120882:sre-agent-work",
        observed_problem="A sampled dead-letter message is not transient.",
        problem_code="sqs.dlq_poison",
        health="unhealthy",
        evidence=[breach],
    )
    assert problem_identity(transient) == "sqs.dlq_transient"
    assert problem_identity(other) == "catalog.breach.SQS-S04"
    assert problem_identity(transient) != problem_identity(other)


def test_placeholder_catalog_failure_does_not_create_a_jira_issue():
    diagnosis = Diagnosis(
        incident_id="inc-1",
        resource_id="api-a",
        observed_problem="Could not read catalog rule APIGW-S02: Placeholder ${api_id} has no value. The access log group is missing.",
        problem_code="catalog.symptom",
        health="degraded",
        dedup_key="111|us-east-2|apigateway:restapi|catalog.symptom",
    )
    jira = FakeJira()
    assert jira.sync(diagnosis) == ""
    assert jira.last_created is False
    assert jira.issues == {}


def test_healthy_resource_does_not_create_a_jira_issue():
    diagnosis = Diagnosis(
        incident_id="inc-1",
        resource_id="fn-a",
        observed_problem="No fault is currently observed.",
        problem_code="healthy",
        health="healthy",
        status="resolved",
    )
    jira = FakeJira()
    assert jira.sync(diagnosis) == ""
    assert jira.issues == {}


def test_a_real_breach_on_two_resources_is_one_ticket():
    from sre_agent.integrations.jira import proposed_solution

    observed = "Catalog rule APIGW-S02 v2: 5XXError is 4 GreaterThanThreshold 0."
    first = Diagnosis(
        incident_id="inc-1",
        resource_id="api-a",
        observed_problem=observed,
        problem_code="catalog.symptom",
        health="unhealthy",
        dedup_key="111|us-east-2|apigateway:restapi|catalog.symptom",
    )
    first.where_seen = [
        "111|us-east-2|apigateway:restapi|api-a",
        "111|us-east-2|apigateway:restapi|api-b",
    ]
    second = Diagnosis(
        incident_id="inc-2",
        resource_id="api-b",
        observed_problem=observed,
        problem_code="catalog.symptom",
        health="unhealthy",
        dedup_key=first.dedup_key,
    )
    second.where_seen = list(first.where_seen)
    jira = FakeJira()
    key = jira.sync(first)
    assert jira.last_created is True
    assert jira.sync(second) == key
    assert len(jira.issues) == 1
    text = jira.issues[key].description
    assert "111|us-east-2|apigateway:restapi|api-a" in text
    assert "111|us-east-2|apigateway:restapi|api-b" in text
    solution = proposed_solution(first)
    assert "5XXError" in solution
    assert "APIGW-S02" in solution
    assert solution in text


def test_false_alarms_do_not_open_or_update_jira_and_a_verified_close_still_works():
    jira = FakeJira()
    opened = jira.sync(
        Diagnosis(
            incident_id="old",
            resource_id="queue",
            observed_problem="Catalog rule SQS-S05 cannot be evaluated because dlq has no value.",
            problem_code="catalog.symptom",
            health="degraded",
            dedup_key="problem|catalog.placeholder.SQS-S05",
        )
    )
    assert opened == ""
    assert jira.issues == {}
    planted = Diagnosis(
        incident_id="planted",
        resource_id="api",
        observed_problem="Catalog rule APIGW-S02 cannot be evaluated because apigw_access_log_group has no value.",
        problem_code="catalog.symptom",
        health="degraded",
        dedup_key="problem|placeholder",
    )
    planted.jira_issue_key = ""
    # A ticket filed before the guard is closed when the same false alarm is seen again.
    jira.issues["KAN-56"] = JiraIssue(
        key="KAN-56",
        dedup_key="problem|placeholder",
        summary=planted.observed_problem,
        description=f"* Issue\n{planted.observed_problem}\n",
    )
    assert false_alarm_text(planted.observed_problem)
    assert issue_is_actionable(planted) is False
    jira.sync(planted)
    assert jira.issues["KAN-56"].status == "Done"
    healthy = Diagnosis(
        incident_id="ok",
        resource_id="fn",
        observed_problem="No fault is currently observed.",
        problem_code="healthy",
        health="healthy",
        status="resolved",
    )
    assert jira.sync(healthy) == ""
