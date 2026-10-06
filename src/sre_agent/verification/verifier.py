"""Recovery is a later observation, not the repair API's response.

Cancelling a running move does not return messages that already moved.
A live clock waits for the window. A simulated clock steps forward for tests.
"""

from __future__ import annotations

from collections.abc import Callable

from sre_agent.clock import parse_time
from sre_agent.models import ActionRecord, HealthAssessment, VerificationResult


class RecoveryVerifier:
    def __init__(self, clock, window_seconds: int, timeout_seconds: int = 900) -> None:
        self.clock = clock
        self.window_seconds = window_seconds
        self.timeout_seconds = timeout_seconds

    def verify(
        self,
        action: ActionRecord,
        reassess: Callable[[str, object, object], HealthAssessment],
        rollback: Callable[[ActionRecord], str],
        processed: Callable[[ActionRecord], set[str]] | None = None,
    ) -> VerificationResult:
        if action.status != "succeeded":
            return VerificationResult(
                action_id=action.action_id,
                incident_id=action.incident_id,
                resource_id=action.resource_id,
                recovered=False,
                status="escalated",
                reasons=["The repair command did not succeed, so there is nothing to verify."],
                rollback="not-needed",
                cancellation="not-needed",
            )
        start = parse_time(action.finished_at)
        timed_out = False
        if not getattr(self.clock, "simulated", True):
            elapsed = (self.clock.now() - start).total_seconds()
            timed_out = elapsed >= self.timeout_seconds
            if elapsed < self.window_seconds and not timed_out:
                return VerificationResult(
                    action_id=action.action_id,
                    incident_id=action.incident_id,
                    resource_id=action.resource_id,
                    recovered=False,
                    status="pending",
                    reasons=["Recovery is waiting for the scheduled verification job and fresh telemetry."],
                    rollback="not-needed",
                    cancellation="not-needed",
                )
        else:
            self.clock.advance(self.window_seconds)
        assessment = reassess(action.resource_id, start, self.clock.now())
        evidence = {"state": assessment.state, "signals": assessment.observed_signals, "reasons": assessment.reasons}
        processing_ok, processing_reason = _processing_confirmed(action, processed)
        healthy = assessment.state == "healthy" and processing_ok
        if healthy:
            return VerificationResult(
                action_id=action.action_id,
                incident_id=action.incident_id,
                resource_id=action.resource_id,
                recovered=True,
                status="recovered",
                reasons=["Health signals and message processing observed after the repair are healthy."],
                rollback="not-needed",
                cancellation="not-needed",
                evidence=evidence,
            )
        if assessment.state in {"degraded", "unhealthy"}:
            reasons = ["The original symptoms are still present."]
        else:
            reasons = ["The verification window has no healthy telemetry, so recovery is not confirmed."]
        if timed_out:
            reasons.insert(0, "Verification timed out before recovery was confirmed.")
        if not processing_ok:
            reasons.append(processing_reason)
        reasons.extend(assessment.reasons)
        cancellation = rollback(action)
        return VerificationResult(
            action_id=action.action_id,
            incident_id=action.incident_id,
            resource_id=action.resource_id,
            recovered=False,
            status="escalated",
            reasons=reasons,
            rollback="not-possible" if action.runbook_id == "sqs.redrive_dlq" else cancellation,
            cancellation=cancellation,
            evidence=evidence,
        )


def _processing_confirmed(action: ActionRecord, processed: Callable[[ActionRecord], set[str]] | None) -> tuple[bool, str]:
    if action.runbook_id != "sqs.redrive_dlq":
        return True, ""
    if not action.order_ids:
        return False, "No application order ids were recorded, so queue health alone is not recovery."
    done = set() if processed is None else set(processed(action))
    missing = [item for item in action.order_ids if item not in done]
    if missing:
        return False, f"Order {missing[0]} was not processed after the repair."
    return True, ""
