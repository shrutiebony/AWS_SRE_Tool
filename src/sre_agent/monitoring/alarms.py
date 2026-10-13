"""CloudWatch alarm state for every discovered resource.

An alarm counts when one of its dimensions names that resource. A directory
function in ALARM is unhealthy on its own, so the checker can open a ticket.
Other resources still need a current fault as well. OK and INSUFFICIENT_DATA
do not count.
"""

from __future__ import annotations

from sre_agent.aws.errors import AccessDenied
from sre_agent.models import HealthAssessment, ResourceRecord

# Dimension names this checker already uses, plus TopicName for SNS.
DIMENSION_TYPES = {
    "FunctionName": "lambda:function",
    "QueueName": "sqs:queue",
    "InstanceId": "ec2:instance",
    "TableName": "dynamodb:table",
    "ApiName": "apigateway:restapi",
    "TopicName": "sns:topic",
}


def read_metric_alarms(reader, region: str) -> list[dict] | None:
    """Metric alarms in this region.

    None means the read failed. An empty list means the account has none.
    A missing method means this reader has no alarm source, which is not a breach.
    """
    if reader is None or not hasattr(reader, "list_metric_alarms"):
        return []
    try:
        return [dict(item) for item in (reader.list_metric_alarms(region) or [])]
    except AccessDenied:
        return None


def alarm_dimensions(alarm: dict) -> list[tuple[str, str]]:
    """Name/value pairs from a metric alarm, including metric-math metrics."""
    pairs = _pairs(alarm.get("dimensions") if "dimensions" in alarm else alarm.get("Dimensions"))
    for metric in alarm.get("metrics") or alarm.get("Metrics") or []:
        if not isinstance(metric, dict):
            continue
        stat = metric.get("MetricStat") or metric.get("metricStat") or {}
        inner = {}
        if isinstance(stat, dict):
            inner = stat.get("Metric") or stat.get("metric") or {}
        if not inner and isinstance(metric.get("metric"), dict):
            inner = metric["metric"]
        if isinstance(inner, dict):
            pairs.extend(_pairs(inner.get("Dimensions") or inner.get("dimensions")))
    return pairs


def match_alarms(records: list[ResourceRecord], alarms: list[dict]) -> dict[str, list[dict]]:
    """Alarms whose dimensions name a discovered resource of the matching type."""
    index: dict[tuple[str, str], list[ResourceRecord]] = {}
    for record in records:
        if record.presence != "present":
            continue
        for key in _resource_keys(record):
            index.setdefault(key, []).append(record)
    matched: dict[str, list[dict]] = {}
    for alarm in alarms:
        seen: set[str] = set()
        for name, value in alarm_dimensions(alarm):
            resource_type = DIMENSION_TYPES.get(name)
            if not resource_type:
                continue
            for record in index.get((resource_type, value), []):
                if record.resource_id in seen:
                    continue
                seen.add(record.resource_id)
                matched.setdefault(record.resource_id, []).append(alarm)
    return matched


def breaching_names(alarms: list[dict]) -> list[str]:
    """Alarm names in ALARM. OK and INSUFFICIENT_DATA are not breaches."""
    names: list[str] = []
    for alarm in alarms:
        if str(alarm.get("state") or alarm.get("StateValue") or "").upper() != "ALARM":
            continue
        name = str(alarm.get("name") or alarm.get("AlarmName") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def alarm_sentence(names: list[str]) -> str:
    if len(names) == 1:
        return f"CloudWatch alarm {names[0]} is in ALARM."
    listed = ", ".join(names[:-1]) + " and " + names[-1]
    return f"CloudWatch alarms {listed} are in ALARM."


def breaching_alarm_names(assessment) -> list[str]:
    if assessment is None:
        return []
    raw = str((getattr(assessment, "context", None) or {}).get("alarm_names") or "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def alarm_requires_action(record: ResourceRecord) -> bool:
    """A directory Lambda alarm is a fault even when TABLE_NAME is already correct."""
    return record.resource_type == "lambda:function" and record.name in {"sre-dir-lab", "enterprise-sites-directory"}


def has_current_fault(record: ResourceRecord, assessment: HealthAssessment | None) -> bool:
    """True when this resource already shows a live fault, before any alarm is applied.

    A quiet health window and a correct TABLE_NAME are not a fault, even if an
    older CloudWatch evaluation is still ALARM.
    """
    from sre_agent.diagnosis.rules import wrong_directory_table

    if wrong_directory_table(record):
        return True
    if assessment is None or assessment.state not in {"degraded", "unhealthy"}:
        return False
    if str((assessment.context or {}).get("unreadable") or "") == "yes":
        return False
    if str((assessment.context or {}).get("pack") or "") == "none":
        return False
    return any(_is_live_fault_reason(reason) for reason in assessment.reasons or [])


def apply_alarm_health(assessment: HealthAssessment | None, resource_id: str, names: list[str]) -> HealthAssessment:
    """Record a breaching alarm on a resource that already has a live fault."""
    sentence = alarm_sentence(names)
    context = {} if assessment is None else dict(assessment.context)
    context["alarm_state"] = "ALARM"
    context["alarm_names"] = ",".join(names)
    reasons = [] if assessment is None else [reason for reason in assessment.reasons if not str(reason).startswith("CloudWatch alarm")]
    if sentence not in reasons:
        reasons.insert(0, sentence)
    return HealthAssessment(
        resource_id,
        "unhealthy",
        reasons,
        [] if assessment is None else list(assessment.missing_signals),
        {} if assessment is None else dict(assessment.observed_signals),
        context,
    )


def _is_live_fault_reason(reason: str) -> bool:
    """A pack or catalog breach. Quiet series, stale samples, and missing reads are not faults."""
    text = str(reason or "").strip()
    if not text or text.startswith("CloudWatch alarm"):
        return False
    ignored = (
        "No invocations",
        "The latest invocations succeeded",
        "Older errors",
        "Queue age and depth are within bounds",
        "Table throttles",
        "API errors and latency are within bounds",
        "Status checks are passing",
        "Memory and disk were not observed",
        "Could not read",
        "No monitoring pack",
        "The newest Lambda sample is older",
        "Sparse metrics omit",
    )
    return not text.startswith(ignored)


def _resource_keys(record: ResourceRecord) -> list[tuple[str, str]]:
    keys = []
    name = str(record.name or "")
    if name:
        keys.append((record.resource_type, name))
    instance = str((record.config or {}).get("instance_id") or "")
    if instance and (record.resource_type, instance) not in keys:
        keys.append((record.resource_type, instance))
    return keys


def _pairs(dimensions) -> list[tuple[str, str]]:
    if isinstance(dimensions, dict):
        return [(str(key), str(value)) for key, value in dimensions.items() if key and value]
    pairs = []
    for item in dimensions or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or item.get("name") or "")
        value = str(item.get("Value") or item.get("value") or "")
        if name and value:
            pairs.append((name, value))
    return pairs
