"""Evaluate versioned Confluence rules, including metric math and log queries."""

from __future__ import annotations

import ast

from sre_agent.aws.errors import CloudError
from sre_agent.integrations.confluence import CatalogRule, ConfluenceError
from sre_agent.models import HealthAssessment


def apply_catalog(assessment: HealthAssessment, rules: list[CatalogRule], record, reader, start, end) -> HealthAssessment:
    catalog_only = assessment.context.get("pack") == "none"
    reasons = [] if catalog_only else list(assessment.reasons)
    state = "healthy" if catalog_only else assessment.state
    observed = dict(assessment.observed_signals)
    missing = list(assessment.missing_signals)
    matched = []
    for rule in rules:
        if rule.resource_type != record.resource_type:
            continue
        try:
            matched.append(rule.resolved(_placeholders(record)))
        except ConfluenceError as exc:
            state = _worse(state, "degraded")
            reasons.append(f"Could not read catalog rule {rule.rule_id}: {exc}")
    if catalog_only and not matched:
        return assessment
    for rule in matched:
        try:
            value, detail = _measure(rule, record, reader, start, end)
        except CloudError as exc:
            state = _worse(state, "degraded")
            missing.append(rule.signal)
            reasons.append(f"Could not read {rule.signal} for catalog rule {rule.rule_id} v{rule.version} ({exc}).")
            continue
        if value is None:
            continue
        try:
            threshold = metric_math(rule.threshold_expression, {}) if rule.threshold_expression else rule.threshold
        except CloudError as exc:
            state = _worse(state, "degraded")
            reasons.append(f"Could not read the threshold for catalog rule {rule.rule_id} v{rule.version} ({exc}).")
            continue
        observed[rule.signal] = float(value)
        if _breached(value, rule.comparison, threshold):
            state = _worse(state, "degraded" if rule.role == "supporting" else "unhealthy")
            reasons.append(
                f"Catalog rule {rule.rule_id} v{rule.version}: {rule.signal} is {value} {rule.comparison} {rule.threshold}. {detail}".strip()
            )
    if state not in {"healthy", "degraded", "unhealthy"}:
        state = "degraded"
        reasons.append(f"Could not read {', '.join(missing) or 'a catalog signal'}.")
    return HealthAssessment(record.resource_id, state, reasons, missing, observed, dict(assessment.context))


def _measure(rule: CatalogRule, record, reader, start, end) -> tuple[float | None, str]:
    period = rule.period_seconds or 300
    if rule.statistic == "Expression":
        names = _names(rule.expression)
        raw = reader.get_metric_data(
            record.account_id,
            record.region,
            record.resource_id,
            names,
            start,
            end,
            stat="Sum",
            period=period,
            namespace=rule.namespace or None,
            dimensions=_dimensions(rule.dimension),
        )
        values = {name: aggregate_points(raw.get(name, []), "Sum", period) for name in names}
        if any(value is None for value in values.values()):
            return None, ""
        return metric_math(rule.expression, {name: float(value) for name, value in values.items()}), rule.expression
    if rule.log_query:
        rows = reader.run_logs_query(
            record.account_id,
            record.region,
            record.resource_id,
            rule.log_query,
            start,
            end,
            log_group=rule.namespace or None,
        )
        return interpret_log_rows(rows, rule.statistic), rule.log_query
    raw = reader.get_metric_data(
        record.account_id,
        record.region,
        record.resource_id,
        [rule.signal],
        start,
        end,
        stat=rule.statistic,
        period=period,
        namespace=rule.namespace or None,
        dimensions=_dimensions(rule.dimension),
    )
    return aggregate_points(raw.get(rule.signal, []), rule.statistic, period), f"{rule.statistic} over {period}s"


def metric_math(expression: str, values: dict[str, float]) -> float:
    tree = ast.parse(expression, mode="eval")
    return float(_eval(tree.body, values))


def _eval(node, values: dict[str, float]) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise CloudError(f"Metric {node.id} was not supplied")
        return values[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left = _eval(node.left, values)
        right = _eval(node.right, values)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise CloudError("Metric math divided by zero")
        return left / right
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, values)
    raise CloudError("Metric math only allows numbers and + - * /")


def _names(expression: str) -> list[str]:
    tree = ast.parse(expression, mode="eval")
    return sorted({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})


def aggregate_points(points, statistic: str, period_seconds: int) -> float | None:
    """Apply the catalog statistic across period buckets instead of keeping the last point."""
    if not points:
        return None
    period = max(int(period_seconds or 60), 1)
    buckets: dict[int, list[float]] = {}
    for at, value in points:
        stamp = at.timestamp() if hasattr(at, "timestamp") else float(at)
        buckets.setdefault(int(stamp) // period, []).append(float(value))
    reduced = [_reduce_bucket(values, statistic) for values in buckets.values()]
    if statistic == "Sum":
        return float(sum(reduced))
    if statistic == "Average":
        return float(sum(reduced) / len(reduced))
    if statistic == "Maximum":
        return float(max(reduced))
    if statistic == "Minimum":
        return float(min(reduced))
    if statistic == "Count":
        return float(sum(sum(values) for values in buckets.values()))
    raise CloudError(statistic)


def interpret_log_rows(rows: list[dict], statistic: str) -> float:
    """A Logs Insights stats row carries the aggregate. Counting those rows is not the count."""
    if not rows:
        return 0.0
    if len(rows) == 1:
        lowered = {str(key).lstrip("@").lower(): value for key, value in rows[0].items()}
        for key in ("value", "count", "cnt", "sum", "avg", "average", "min", "max"):
            if key in lowered and lowered[key] not in ("", None):
                return float(lowered[key])
    if statistic == "Count":
        return float(len(rows))
    if statistic == "Sum":
        return float(sum(float(row.get("value") or 0) for row in rows))
    if statistic == "Maximum":
        return float(max(float(row.get("value") or 0) for row in rows))
    if statistic == "Minimum":
        return float(min(float(row.get("value") or 0) for row in rows))
    if statistic == "Average":
        values = [float(row.get("value") or 0) for row in rows]
        return float(sum(values) / len(values))
    return float(len(rows))


def _reduce_bucket(values: list[float], statistic: str) -> float:
    if statistic == "Average":
        return sum(values) / len(values)
    if statistic == "Maximum":
        return max(values)
    if statistic == "Minimum":
        return min(values)
    if statistic == "Count":
        return float(len(values))
    return float(sum(values))


def _breached(value: float, comparison: str, threshold: float) -> bool:
    if comparison == "GreaterThanThreshold":
        return value > threshold
    if comparison == "GreaterThanOrEqualToThreshold":
        return value >= threshold
    if comparison == "LessThanThreshold":
        return value < threshold
    raise CloudError(comparison)


def _worse(current: str, new: str) -> str:
    rank = {"healthy": 0, "degraded": 1, "unhealthy": 2}
    if current not in rank:
        current = "degraded"
    if new not in rank:
        new = "degraded"
    return new if rank[new] > rank[current] else current


def _dimensions(text: str) -> list[dict[str, str]] | None:
    if not text.strip():
        return None
    dimensions = []
    for part in text.split(","):
        name, separator, value = part.partition("=")
        if not separator or not name.strip() or not value.strip():
            raise CloudError(f"Dimension {text} must look like Name={{name}}")
        dimensions.append({"Name": name.strip(), "Value": value.strip()})
    return dimensions


def _placeholders(record) -> dict[str, str]:
    values = {
        "name": record.name,
        "resource_id": record.resource_id,
        "region": record.region,
        "account_id": record.account_id,
        "queue": record.name,
        "queue_name": record.name,
        "function": record.name,
        "function_name": record.name,
        "worker_function": record.name,
        "table": record.name,
        "table_name": record.name,
        "api_name": record.name,
    }
    timeout = record.config.get("timeout")
    if timeout:
        values["timeout_ms"] = str(int(float(timeout) * 1000))
    visibility = record.config.get("visibility_timeout") or record.config.get("visibility_timeout_seconds")
    if visibility:
        values["visibility_timeout_ms"] = str(int(float(visibility) * 1000))
    reserved = record.config.get("reserved_concurrency")
    if reserved:
        values["reserved_concurrency"] = str(reserved)
    policy = record.config.get("redrive_policy") or {}
    if isinstance(policy, dict) and policy.get("deadLetterTargetArn"):
        values["dlq"] = str(policy["deadLetterTargetArn"]).rsplit(":", 1)[-1]
    return values
