from sre_agent.integrations.jira import FakeJira, star_plain
from sre_agent.models import Diagnosis, ProposedAction


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


def test_one_problem_on_two_resources_is_one_ticket():
    from sre_agent.integrations.jira import proposed_solution

    first = Diagnosis(
        incident_id="inc-1",
        resource_id="api-a",
        observed_problem="Could not read catalog rule APIGW-S02: Placeholder ${api_id} has no value. The access log group is missing.",
        problem_code="catalog.symptom",
        health="degraded",
        dedup_key="111|us-east-2|apigateway:restapi|catalog.symptom",
    )
    first.where_seen = [
        "111|us-east-2|apigateway:restapi|api-a",
        "111|us-east-2|apigateway:restapi|api-b",
    ]
    second = Diagnosis(
        incident_id="inc-2",
        resource_id="api-b",
        observed_problem=first.observed_problem,
        problem_code="catalog.symptom",
        health="degraded",
        dedup_key=first.dedup_key,
    )
    second.where_seen = list(first.where_seen)
    jira = FakeJira()
    key = jira.sync(first)
    assert jira.sync(second) == key
    assert len(jira.issues) == 1
    text = jira.issues[key].description
    assert "111|us-east-2|apigateway:restapi|api-a" in text
    assert "111|us-east-2|apigateway:restapi|api-b" in text
    solution = proposed_solution(first)
    assert "api_id" in solution
    assert "access log group" in solution
    assert "confirm the metric is publishing" in solution
    assert solution in text
