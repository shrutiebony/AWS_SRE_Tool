"""Multi-agent allowlists, explanation guardrails, and the MCP tool surface."""

import io
import json

from sre_agent.agents.guardrails import GuardrailRejection, accept_explanation
from sre_agent.agents.narrator import explain
from sre_agent.agents.workflow import ToolBroker, ToolDenied
from sre_agent.lab.scenarios import build_world
from sre_agent.mcp_server import McpSession, read_message, respond
from sre_agent.models import AgentConfig, Diagnosis, Evidence, ProposedAction
from sre_agent.registry.store import Registry
from sre_agent.service import OperationsAgent


def _diagnosis() -> Diagnosis:
    return Diagnosis(
        incident_id="inc",
        resource_id="arn:aws:sqs:us-east-1:123456789012:payments",
        observed_problem="Dead-letter queue is holding messages after transient consumer errors.",
        problem_code="sqs.dlq_transient",
        health="unhealthy",
        evidence=[Evidence("ev-logs", "logs", "Failure class from logs is transient.")],
        proposed_action=ProposedAction(
            "sqs.redrive_dlq",
            "arn:aws:sqs:us-east-1:123456789012:payments",
            "Replay the transient orders.",
            ["ev-logs"],
            True,
        ),
    )


def test_model_cannot_invent_an_action_or_a_resource():
    diagnosis = _diagnosis()
    try:
        accept_explanation(
            diagnosis,
            {
                "summary": "Run sqs.purge_queue on arn:aws:sqs:us-east-1:123456789012:other.",
                "evidence_ids": ["ev-logs"],
                "resource_id": diagnosis.resource_id,
                "problem_code": diagnosis.problem_code,
                "action": "sqs.redrive_dlq",
            },
        )
    except GuardrailRejection as exc:
        assert "action" in str(exc) or "resource" in str(exc)
    else:
        raise AssertionError("invented action was accepted")


def test_model_cannot_change_the_action_field():
    diagnosis = _diagnosis()
    try:
        accept_explanation(
            diagnosis,
            {
                "summary": "The dead-letter queue is holding transient failures.",
                "evidence_ids": ["ev-logs"],
                "resource_id": diagnosis.resource_id,
                "problem_code": diagnosis.problem_code,
                "action": "none",
            },
        )
    except GuardrailRejection as exc:
        assert "action" in str(exc)
    else:
        raise AssertionError("changed action was accepted")


def test_grounded_explanation_is_kept_and_a_mutation_is_reverted():
    diagnosis = _diagnosis()

    def complete(_prompt: str) -> str:
        diagnosis.proposed_action = None
        diagnosis.problem_code = "sqs.dlq_poison"
        return json.dumps(
            {
                "summary": "Dead-letter queue is holding messages after transient consumer errors.",
                "evidence_ids": ["ev-made-up"],
                "resource_id": "arn:aws:sqs:us-east-1:123456789012:payments",
                "problem_code": "sqs.dlq_poison",
                "action": "none",
            }
        )

    note = explain(diagnosis, complete)
    assert note.source == "rules"
    assert note.rejected
    assert diagnosis.problem_code == "sqs.dlq_transient"
    assert diagnosis.proposed_action is not None
    assert diagnosis.proposed_action.runbook_id == "sqs.redrive_dlq"


def test_narrator_cannot_call_the_repair_tool():
    broker = ToolBroker()
    try:
        broker.call("narrator", "execute", lambda: None)
    except ToolDenied as exc:
        assert "narrator" in str(exc)
    else:
        raise AssertionError("narrator executed a repair")


def test_workflow_recommends_and_records_each_agent():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock, AgentConfig(mode="recommend"))
    report = agent.run_cycle()
    names = [step.agent for step in report.agent_steps]
    assert names == ["discovery", "monitoring", "diagnosis", "narrator", "policy", "repair", "verification"]
    assert report.agent_steps[5].status == "skipped"
    assert report.actions == []
    assert any(item.problem_code == "sqs.dlq_transient" for item in report.diagnoses)
    assert all(note.source == "rules" for note in report.narrations)
    assert all(note.rejected == "" for note in report.narrations)


def test_mcp_repair_tool_refuses_a_recommendation():
    session = McpSession()
    listed = respond(session, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [item["name"] for item in listed["result"]["tools"]]
    assert names == ["sre_run_cycle", "sre_explain", "sre_repair"]
    reply = respond(session, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "sre_repair", "arguments": {"runbook_id": "sqs.purge_queue"}}})
    text = reply["result"]["content"][0]["text"]
    assert text.startswith("Refused.")
    assert session.report.actions == []


def test_mcp_framing_reads_a_content_length_message():
    payload = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode()
    stream = io.BytesIO(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
    message = read_message(stream)
    reply = respond(McpSession(), message)
    assert reply["id"] == 7
    assert reply["result"] == {}
