"""Read CloudWatch-style samples and apply the pack for a resource type."""

from __future__ import annotations

from datetime import datetime

from sre_agent.aws.errors import AccessDenied
from sre_agent.aws.paging import with_backoff
from sre_agent.models import HealthAssessment, ResourceRecord
from sre_agent.monitoring.packs import assess, optional_signals, required_signals

COUNTERS = {"Errors", "Invocations", "Throttles"}


def sample(
    record: ResourceRecord,
    reader,
    start: datetime,
    end: datetime,
    start_exclusive: bool = False,
) -> HealthAssessment:
    names = list(dict.fromkeys([*required_signals(record), *optional_signals(record)]))
    context: dict[str, str] = {}
    if reader is None:
        return HealthAssessment(
            resource_id=record.resource_id,
            state="unknown",
            reasons=["No credentials for this account. This does not mean the resource is healthy."],
            missing_signals=required_signals(record),
        )
    own_names = [name for name in names if not name.startswith("dlq.")]
    try:
        raw = with_backoff(
            lambda: reader.get_metric_data(record.account_id, record.region, record.resource_id, own_names, start, end)
        )
    except AccessDenied as exc:
        return HealthAssessment(
            resource_id=record.resource_id,
            state="unknown",
            reasons=[f"Metrics could not be read ({exc}). This does not mean the resource is healthy."],
            missing_signals=required_signals(record),
        )
    signals: dict[str, float | None] = {
        name: _reduce(name, raw.get(name, []), start, end, start_exclusive) for name in own_names
    }
    dlq_arn = ""
    policy = record.config.get("redrive_policy") or {}
    if isinstance(policy, dict):
        dlq_arn = str(policy.get("deadLetterTargetArn") or "")
    if dlq_arn:
        context["dlq_resource_id"] = dlq_arn
        try:
            dlq_raw = with_backoff(
                lambda: reader.get_metric_data(
                    record.account_id,
                    record.region,
                    dlq_arn,
                    ["ApproximateNumberOfMessagesVisible"],
                    start,
                    end,
                )
            )
        except AccessDenied as exc:
            return HealthAssessment(
                resource_id=record.resource_id,
                state="unknown",
                reasons=[f"Dead-letter metrics could not be read ({exc}). This does not mean the queue is healthy."],
                missing_signals=required_signals(record),
            )
        points = dlq_raw.get("ApproximateNumberOfMessagesVisible", [])
        signals["dlq.ApproximateNumberOfMessagesVisible"] = _reduce(
            "dlq.ApproximateNumberOfMessagesVisible",
            points,
            start,
            end,
            start_exclusive,
        )
        growth = _growth(points, start, end, start_exclusive)
        context["dlq_growth"] = "unknown" if growth is None else str(growth)
    if record.resource_type == "lambda:function":
        context.update(_lambda_freshness(raw, start, end, start_exclusive))
    return assess(record, signals, context)


def _lambda_freshness(raw, start: datetime, end: datetime, start_exclusive: bool) -> dict[str, str]:
    """Latest period only. A summed lookback would keep a restored consumer looking failed."""
    series = {
        "latest_errors": raw.get("Errors", []),
        "latest_invocations": raw.get("Invocations", []),
        "latest_throttles": raw.get("Throttles", []),
    }
    found: dict[str, str] = {}
    stamps = []
    for key, points in series.items():
        stamp, value = _latest_point(points, start, end, start_exclusive)
        if value is None:
            continue
        found[key] = str(value)
        if stamp is not None:
            stamps.append(stamp)
    if stamps:
        newest = max(stamps)
        found["latest_metric_age_seconds"] = str(max(0.0, (end - newest).total_seconds()))
    return found


def _latest_point(points, start: datetime, end: datetime, start_exclusive: bool):
    selected = [(stamp, value) for stamp, value in points if _in_window(stamp, start, end, start_exclusive)]
    if not selected:
        return None, None
    stamp, value = max(selected, key=lambda item: item[0])
    return stamp, float(value)


def _reduce(name: str, points, start: datetime, end: datetime, start_exclusive: bool) -> float | None:
    selected = [value for stamp, value in points if _in_window(stamp, start, end, start_exclusive)]
    if not selected:
        return None
    metric = name.split(".", 1)[-1]
    if metric in COUNTERS:
        return float(sum(selected))
    latest = max(
        ((stamp, value) for stamp, value in points if _in_window(stamp, start, end, start_exclusive)),
        key=lambda item: item[0],
    )
    return float(latest[1])


def _growth(points, start, end, start_exclusive) -> float | None:
    selected = [(stamp, value) for stamp, value in points if _in_window(stamp, start, end, start_exclusive)]
    if len(selected) < 2:
        return None
    selected.sort(key=lambda item: item[0])
    return float(selected[-1][1] - selected[0][1])


def _in_window(stamp: datetime, start: datetime, end: datetime, start_exclusive: bool) -> bool:
    if start_exclusive:
        return start < stamp <= end
    return start <= stamp <= end
