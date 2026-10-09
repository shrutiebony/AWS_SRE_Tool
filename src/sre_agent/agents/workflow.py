"""Run discovery, monitoring, diagnosis, policy, repair, and verification as separate agents.

Each agent may call only its own tool. The narrator receives the evidence packet
and cannot call a repair. The repair agent runs only after the policy gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from sre_agent.agents.narrator import Narration, explain


class ToolDenied(PermissionError):
    """An agent called a tool outside its allowlist."""


@dataclass(frozen=True)
class AgentStep:
    agent: str
    tool: str
    status: str


class ToolBroker:
    grants = {
        "discovery": frozenset({"inventory"}),
        "monitoring": frozenset({"monitor"}),
        "diagnosis": frozenset({"diagnose"}),
        "narrator": frozenset({"explain"}),
        "policy": frozenset({"review"}),
        "repair": frozenset({"execute"}),
        "verification": frozenset({"verify"}),
    }

    def call(self, agent: str, tool: str, fn):
        if tool not in self.grants.get(agent, frozenset()):
            raise ToolDenied(f"{agent} cannot call {tool}")
        return fn()


def run_guarded_cycle(ops, complete=None):
    """Same cycle as before, with an agent trace and a grounded explanation."""
    from sre_agent.service import CycleReport

    broker = ToolBroker()
    steps: list[AgentStep] = []

    def step(agent: str, tool: str, fn, skip: bool = False):
        if skip:
            steps.append(AgentStep(agent, tool, "skipped"))
            return None
        result = broker.call(agent, tool, fn)
        steps.append(AgentStep(agent, tool, "ok"))
        return result

    step("discovery", "inventory", ops.inventory)
    assessments = step("monitoring", "monitor", ops.monitor)
    diagnoses = step("diagnosis", "diagnose", ops.diagnose_open)
    narrations = step("narrator", "explain", lambda: [explain(item, complete) for item in diagnoses])
    decisions = step("policy", "review", lambda: ops.review(diagnoses))
    approved = [item for item in decisions if item.outcome == "execute"]
    actions = step("repair", "execute", lambda: ops.execute(approved), skip=not approved)
    if actions is None:
        actions = []
    verifications = step("verification", "verify", lambda: ops.verify(actions), skip=not actions)
    if verifications is None:
        verifications = []
    ops._note_agent_health()
    stored = {item.incident_id: item for item in ops.registry.incidents()}
    diagnoses = [stored.get(item.incident_id, item) for item in diagnoses]
    return CycleReport(
        scans=ops.registry.scans(),
        resources=ops.registry.list_resources(),
        assessments=assessments,
        diagnoses=diagnoses,
        decisions=decisions,
        actions=actions,
        verifications=verifications,
        escalations=ops.registry.escalations(),
        outcomes=ops.registry.outcomes(),
        agent_steps=steps,
        narrations=narrations,
    )
