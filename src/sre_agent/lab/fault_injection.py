"""Lab validation for the redrive runbook.

Production monitoring never imports this module. It observes failures; it does not create them.
"""

from __future__ import annotations

from sre_agent.aws.fake import FakeCloud
from sre_agent.lab.scenarios import build_world, publish_recovery
from sre_agent.models import AgentConfig
from sre_agent.registry.store import Registry
from sre_agent.service import OperationsAgent


def run_validation() -> list[tuple[str, bool, str]]:
    checks = [
        ("recommendation mode does not repair", _recommendation_does_not_repair),
        ("transient redrive recovers only after fresh telemetry", _transient_recovers),
        ("API success without recovery escalates and stops", _api_success_is_not_recovery),
        ("poison messages are not redriven", _poison_blocked),
        ("a failing consumer is not redriven", _consumer_failure_blocked),
    ]
    results = []
    for name, check in checks:
        try:
            detail = check()
            results.append((name, True, detail))
        except Exception as exc:
            results.append((name, False, str(exc)))
    return results


def _agent(world, **config):
    if not isinstance(world.cloud, FakeCloud):
        raise RuntimeError("Fault injection is a lab tool and cannot run against live AWS.")
    return OperationsAgent(world.scope, Registry(), world.clock, AgentConfig(**config))


def _recommendation_does_not_repair() -> str:
    world = build_world()
    report = _agent(world, mode="recommend").run_cycle()
    proposal = _proposal(report)
    if proposal is None or not proposal.automatic_eligible:
        raise AssertionError("expected a redrive recommendation")
    if report.actions:
        raise AssertionError("recommendation mode executed a repair")
    if "replay_orders" in world.cloud.calls:
        raise AssertionError("the executor ran")
    if world.cloud.queues["payments-dlq"].messages != 2:
        raise AssertionError("queue messages changed")
    return "proposed sqs.redrive_dlq and left the queue untouched"


def _transient_recovers() -> str:
    world = build_world()
    agent = _agent(world, mode="automatic", enabled_automatic_runbooks={"sqs.redrive_dlq"})
    agent.inventory()
    agent.monitor()
    diagnoses = agent.diagnose_open()
    decisions = agent.review(diagnoses)
    actions = agent.execute([item for item in decisions if item.outcome == "execute"])
    if len(actions) != 1 or actions[0].status != "succeeded":
        raise AssertionError(f"expected one successful API call, got {actions}")
    if world.cloud.queues["payments-dlq"].messages != 0:
        raise AssertionError("the move API did not take effect")
    publish_recovery(world)
    results = agent.verify(actions)
    if not results[0].recovered:
        raise AssertionError(results[0].reasons)
    return "redrive API succeeded and later telemetry showed recovery"


def _api_success_is_not_recovery() -> str:
    world = build_world()
    agent = _agent(world, mode="automatic", enabled_automatic_runbooks={"sqs.redrive_dlq"})
    report = agent.run_cycle()
    action = _one_action(report)
    if action.status != "succeeded":
        raise AssertionError(action.api_result)
    verification = report.verifications[0]
    if verification.recovered or verification.rollback != "not-possible" or verification.cancellation != "already-finished":
        raise AssertionError(verification)
    record = agent.registry.get_by_resource_id(world.queue_id)
    if "sqs.redrive_dlq" not in record.runbook_blocks:
        raise AssertionError("failed recovery did not stop repeats")
    again = agent.run_cycle()
    if any(item.outcome == "execute" for item in again.decisions):
        raise AssertionError("a blocked runbook ran again")
    if world.cloud.calls.count("replay_orders") != 1:
        raise AssertionError("the move API ran more than once")
    return "API success without healthy telemetry escalated and blocked a repeat"


def _poison_blocked() -> str:
    world = build_world(poison=True)
    report = _agent(world, mode="automatic", enabled_automatic_runbooks={"sqs.redrive_dlq"}).run_cycle()
    if any(item.outcome == "execute" for item in report.decisions):
        raise AssertionError("poison messages were redriven")
    codes = {item.problem_code for item in report.diagnoses}
    if "sqs.dlq_poison" not in codes:
        raise AssertionError(codes)
    return "poison evidence blocked the runbook"


def _consumer_failure_blocked() -> str:
    world = build_world(consumer_unhealthy=True)
    report = _agent(world, mode="automatic", enabled_automatic_runbooks={"sqs.redrive_dlq"}).run_cycle()
    if any(item.outcome == "execute" for item in report.decisions):
        raise AssertionError("redrive ran while the consumer was unhealthy")
    codes = {item.problem_code for item in report.diagnoses}
    if "sqs.backlog_consumer_failing" not in codes:
        raise AssertionError(codes)
    return "consumer failure was diagnosed instead of redrive"


def _proposal(report):
    for diagnosis in report.diagnoses:
        if diagnosis.proposed_action is not None:
            return diagnosis.proposed_action
    return None


def _one_action(report):
    succeeded = [item for item in report.actions if item.status == "succeeded"]
    if len(succeeded) != 1:
        raise AssertionError(report.actions)
    return succeeded[0]


def main() -> int:
    results = run_validation()
    failed = False
    print("Lab validation for sqs.redrive_dlq")
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        failed = failed or not ok
        print(f"  {mark}  {name}: {detail}")
    return 1 if failed else 0
