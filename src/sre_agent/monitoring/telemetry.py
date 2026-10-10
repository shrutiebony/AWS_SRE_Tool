"""Read CloudWatch-style samples and apply the pack for a resource type."""

from __future__ import annotations

from datetime import datetime, timedelta

from sre_agent.aws.errors import AccessDenied
from sre_agent.aws.names import parse_arn, resource_name
from sre_agent.aws.paging import with_backoff
from sre_agent.models import HealthAssessment, ResourceRecord
from sre_agent.monitoring.packs import assess, optional_signals, required_signals

COUNTERS = {"Errors", "Invocations", "Throttles", "5XXError", "Count", "ReadThrottleEvents", "WriteThrottleEvents", "UserErrors"}
# CloudWatch omits these when the value is zero. An empty successful read is zero, not a missing signal.
_ZERO_WHEN_ABSENT = {
    "lambda:function": {"Errors", "Invocations", "Throttles", "Duration"},
    "dynamodb:table": {"ReadThrottleEvents", "WriteThrottleEvents", "UserErrors"},
    "apigateway:restapi": {"5XXError", "Latency", "Count"},
}


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
        missing = required_signals(record)
        return HealthAssessment(
            resource_id=record.resource_id,
            state="degraded",
            reasons=[f"Could not read {', '.join(missing) or 'required signals'}. No credentials for this account. This does not mean the resource is healthy."],
            missing_signals=missing,
            context={"unreadable": "yes"},
        )
    own_names = [name for name in names if not name.startswith("dlq.")]
    try:
        raw = _load_metrics(record, reader, own_names, start, end)
    except AccessDenied as exc:
        missing = required_signals(record)
        return HealthAssessment(
            resource_id=record.resource_id,
            state="degraded",
            reasons=[f"Could not read {', '.join(missing)} ({exc}). This does not mean the resource is healthy."],
            missing_signals=missing,
            context={"unreadable": "yes"},
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
            missing = required_signals(record)
            return HealthAssessment(
                resource_id=record.resource_id,
                state="degraded",
                reasons=[f"Could not read {', '.join(missing)} ({exc}). This does not mean the queue is healthy."],
                missing_signals=missing,
                context={"unreadable": "yes"},
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
    for name in _ZERO_WHEN_ABSENT.get(record.resource_type, ()):
        if name in signals and signals[name] is None:
            signals[name] = 0.0
    return assess(record, signals, context)


def _load_metrics(record: ResourceRecord, reader, names: list[str], start: datetime, end: datetime) -> dict:
    if record.resource_type != "apigateway:restapi":
        return with_backoff(
            lambda: reader.get_metric_data(record.account_id, record.region, record.resource_id, names, start, end)
        )
    merged = {name: [] for name in names}
    for namespace, dimensions in _api_targets(record, reader):
        raw = with_backoff(
            lambda namespace=namespace, dimensions=dimensions: reader.get_metric_data(
                record.account_id,
                record.region,
                record.resource_id,
                names,
                start,
                end,
                namespace=namespace,
                dimensions=dimensions,
            )
        )
        for name in names:
            merged[name].extend(raw.get(name) or [])
    return merged


def _api_targets(record: ResourceRecord, reader) -> list[tuple[str, list[dict[str, str]]]]:
    """REST API metrics are stored as ApiName plus Stage, not the API id alone."""
    api_name = record.name
    api_id = str((record.config or {}).get("api_id") or "")
    if not api_id:
        try:
            api_id = resource_name(parse_arn(record.resource_id))
        except ValueError:
            api_id = api_name
    stages = [str(item) for item in ((record.config or {}).get("stages") or []) if str(item)]
    if not stages and hasattr(reader, "rest_api_stages") and api_id:
        try:
            stages = [str(item) for item in reader.rest_api_stages(record.region, api_id) if str(item)]
        except Exception:
            stages = []
    if not api_name:
        api_name = api_id
    if stages:
        return [
            (
                "AWS/ApiGateway",
                [{"Name": "ApiName", "Value": api_name}, {"Name": "Stage", "Value": stage}],
            )
            for stage in stages
        ]
    return [("AWS/ApiGateway", [{"Name": "ApiName", "Value": api_name}])]


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


class PrefetchedReader:
    """Serves a batch of CloudWatch series so each resource does not call GetMetricData itself."""

    def __init__(self, inner, metrics: dict, logs: dict) -> None:
        self._inner = inner
        self._metrics = metrics
        self._logs = logs

    def get_metric_data(
        self,
        account_id,
        region,
        resource_id,
        metric_names,
        start,
        end,
        stat=None,
        period=None,
        namespace=None,
        dimensions=None,
    ):
        from sre_agent.aws.boto3_reader import metric_key

        resolved = []
        for name in metric_names:
            key = metric_key(resource_id, name, stat, period, namespace, dimensions)
            if key not in self._metrics:
                return self._inner.get_metric_data(
                    account_id,
                    region,
                    resource_id,
                    metric_names,
                    start,
                    end,
                    stat=stat,
                    period=period,
                    namespace=namespace,
                    dimensions=dimensions,
                )
            resolved.append((name, key))
        return {name: list(self._metrics[key]) for name, key in resolved}

    def run_logs_query(self, account_id, region, resource_id, query, start, end, log_group=None):
        key = (resource_id, query, log_group or "")
        if key not in self._logs:
            return self._inner.run_logs_query(account_id, region, resource_id, query, start, end, log_group=log_group)
        value = self._logs[key]
        if isinstance(value, Exception):
            raise value
        return value

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def prefetched_reader(
    records: list[ResourceRecord],
    reader,
    rules: list,
    start: datetime,
    end: datetime,
    log_seconds: int | None = None,
):
    """Read every pack and catalog series in one GetMetricData, and log rules together."""
    from sre_agent.aws.boto3_reader import metric_key
    from sre_agent.aws.errors import CloudError
    from sre_agent.aws.names import metric_identity
    from sre_agent.aws.paging import with_backoff
    from sre_agent.aws.parallel import parallel_map
    from sre_agent.integrations.confluence import ConfluenceError
    from sre_agent.monitoring.catalog import _dimensions, _names, _placeholders

    queries: list[dict] = []
    metrics: dict = {}
    planned: dict = {}

    def add(region: str, resource_id: str, name: str, stat: str | None, period: int, namespace: str | None, dimensions: list | None) -> None:
        key = metric_key(resource_id, name, stat, period, namespace, dimensions)
        if key in planned or key in metrics:
            return
        namespace_name = key[4]
        dims = [{"Name": item[0], "Value": item[1]} for item in key[5]]
        if not namespace_name or not dims:
            metrics[key] = []
            return
        query_id = f"q{len(queries)}"
        planned[key] = query_id
        queries.append(
            {
                "id": query_id,
                "region": region,
                "namespace": namespace_name,
                "metric_name": name,
                "dimensions": dims,
                "period": key[3],
                "stat": key[2],
            }
        )

    log_jobs = []
    for record in records:
        names = [name for name in dict.fromkeys([*required_signals(record), *optional_signals(record)]) if not name.startswith("dlq.")]
        if record.resource_type == "apigateway:restapi":
            targets = _api_targets(record, reader)
        else:
            try:
                targets = [metric_identity(record.resource_id)]
            except ValueError:
                targets = [(None, None)]
        for namespace, dimensions in targets:
            for name in names:
                add(record.region, record.resource_id, name, None, 60, namespace, dimensions)
        policy = record.config.get("redrive_policy") or {}
        dlq_arn = str(policy.get("deadLetterTargetArn") or "") if isinstance(policy, dict) else ""
        if dlq_arn:
            add(record.region, dlq_arn, "ApproximateNumberOfMessagesVisible", None, 60, None, None)
        for rule in rules:
            if rule.resource_type != record.resource_type:
                continue
            try:
                resolved = rule.resolved(_placeholders(record))
            except ConfluenceError:
                continue
            if resolved.log_query:
                log_jobs.append((record, resolved))
                continue
            try:
                dimensions = _dimensions(resolved.dimension) if resolved.dimension else None
            except CloudError:
                continue
            period = resolved.period_seconds or 300
            namespace = resolved.namespace or None
            if resolved.statistic == "Expression":
                for name in _names(resolved.expression):
                    add(record.region, record.resource_id, name, "Sum", period, namespace, dimensions)
            else:
                add(record.region, record.resource_id, resolved.signal, resolved.statistic, period, namespace, dimensions)

    from concurrent.futures import ThreadPoolExecutor

    def fetch_metrics() -> None:
        if not queries:
            return
        by_region: dict[str, list[dict]] = {}
        for item in queries:
            by_region.setdefault(item["region"], []).append(item)
        found: dict = {}
        for region, chunk in by_region.items():
            page = with_backoff(lambda region=region, chunk=chunk: reader.query_metrics(region, chunk, start, end))
            found.update(page)
        for key, query_id in planned.items():
            metrics[key] = list(found.get(query_id, []))

    logs: dict = {}

    def read_log(job):
        record, resolved = job
        key = (record.resource_id, resolved.log_query, resolved.namespace or "")
        window = int(resolved.period_seconds or 300)
        if log_seconds:
            window = min(window, int(log_seconds))
        log_start = end - timedelta(seconds=window)
        if log_start < start:
            log_start = start
        try:
            rows = reader.run_logs_query(
                record.account_id,
                record.region,
                record.resource_id,
                resolved.log_query,
                log_start,
                end,
                log_group=resolved.namespace or None,
            )
        except Exception as exc:
            return key, exc
        return key, rows

    with ThreadPoolExecutor(max_workers=1) as pool:
        metric_job = pool.submit(fetch_metrics)
        for key, value in parallel_map(read_log, log_jobs, workers=8):
            logs[key] = value
        metric_job.result()
    return PrefetchedReader(reader, metrics, logs)
