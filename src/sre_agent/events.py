"""Event normalization. An alarm starts an investigation. It does not authorize a repair."""

from __future__ import annotations

from sre_agent.aws.names import parse_arn

_DIMENSION_KEYS = {
    "AWS/SQS": "QueueName",
    "AWS/Lambda": "FunctionName",
    "AWS/EC2": "InstanceId",
    "AWS/DynamoDB": "TableName",
    "AWS/ApiGateway": "ApiName",
}
_TYPE_BY_NAMESPACE = {
    "AWS/SQS": "sqs:queue",
    "AWS/Lambda": "lambda:function",
    "AWS/EC2": "ec2:instance",
    "AWS/DynamoDB": "dynamodb:table",
    "AWS/ApiGateway": "apigateway:restapi",
}


class InMemoryQueue:
    def __init__(self) -> None:
        self._items: list[dict] = []

    def send(self, body: dict) -> None:
        self._items.append(dict(body))

    def receive(self) -> dict | None:
        if not self._items:
            return None
        return self._items.pop(0)

    def pending(self) -> list[dict]:
        return [dict(item) for item in self._items]


def normalize_event(event: dict, registry) -> dict | None:
    if event.get("type") in {"refresh", "reconcile", "investigate", "monitor", "verify"}:
        return dict(event)
    detail_type = event.get("detail-type", "")
    detail = event.get("detail") or {}
    if detail_type == "CloudWatch Alarm State Change":
        resource_id = _alarm_resource(event, registry)
        alarm_name = detail.get("alarmName", "alarm")
        if not resource_id:
            return {
                "type": "unmapped",
                "event_name": alarm_name,
                "summary": "Alarm event did not identify a monitored resource. The alarm ARN is not the resource.",
            }
        state = ((detail.get("state") or {}).get("value") or "ALARM")
        return {
            "type": "investigate",
            "resource_id": resource_id,
            "event_name": alarm_name,
            "summary": f"Alarm {alarm_name} entered {state}.",
            "alarm": True,
        }
    if detail_type == "AWS API Call via CloudTrail":
        event_name = detail.get("eventName", "AWS API call")
        resource_id = _from_resources(detail) or _from_response(detail) or _from_request(detail, registry, event)
        summary = f"CloudTrail {event_name}."
        if not resource_id:
            return {"type": "unmapped", "event_name": event_name, "summary": summary}
        return {"type": "refresh", "resource_id": resource_id, "event_name": event_name, "summary": summary}
    return {"type": "unmapped", "event_name": detail_type or "event", "summary": "Event shape is not mapped."}


def _alarm_resource(event: dict, registry) -> str:
    detail = event.get("detail") or {}
    explicit = detail.get("resourceId")
    if explicit:
        return explicit
    linked = ""
    if hasattr(registry, "resource_for_alarm"):
        linked = registry.resource_for_alarm(detail.get("alarmName", ""))
    if linked:
        return linked
    for metric in _alarm_metrics(detail):
        match = _match_metric(metric, registry)
        if match:
            return match
    return ""


def _alarm_metrics(detail: dict) -> list[dict]:
    configuration = detail.get("configuration") or {}
    metrics = []
    for item in configuration.get("metrics") or []:
        stat = item.get("metricStat") or item.get("metric") or {}
        metric = stat.get("metric") or stat
        if metric.get("namespace") or metric.get("name") or metric.get("metricName"):
            metrics.append(metric)
    metric = configuration.get("metric") or detail.get("metric") or {}
    if metric:
        metrics.append(metric)
    return metrics


def _match_metric(metric: dict, registry) -> str:
    namespace = metric.get("namespace") or metric.get("Namespace") or ""
    dimensions = _dimension_map(metric.get("dimensions") or metric.get("Dimensions") or {})
    key = _DIMENSION_KEYS.get(namespace, "")
    value = dimensions.get(key, "")
    resource_type = _TYPE_BY_NAMESPACE.get(namespace, "")
    if not value or not resource_type or not hasattr(registry, "list_resources"):
        return ""
    matches = [
        record.resource_id
        for record in registry.list_resources(resource_type=resource_type)
        if record.name == value or (record.config or {}).get("instance_id") == value
    ]
    if len(matches) == 1:
        return matches[0]
    return ""


def _dimension_map(dimensions) -> dict[str, str]:
    if isinstance(dimensions, dict):
        return {str(key): str(value) for key, value in dimensions.items()}
    mapped = {}
    for item in dimensions or []:
        if isinstance(item, dict):
            name = item.get("name") or item.get("Name") or ""
            value = item.get("value") or item.get("Value") or ""
            if name:
                mapped[str(name)] = str(value)
    return mapped


def _from_resources(detail: dict) -> str:
    for item in detail.get("resources") or []:
        if isinstance(item, dict) and item.get("ARN"):
            arn = item["ARN"]
            if ":alarm:" in arn:
                continue
            return arn
    return ""


def _from_response(detail: dict) -> str:
    response = detail.get("responseElements") or {}
    for key in ("functionArn", "queueArn", "tableArn"):
        if response.get(key):
            return response[key]
    return ""


def _from_request(detail: dict, registry, event: dict) -> str:
    params = detail.get("requestParameters") or {}
    function_name = params.get("functionName")
    if function_name:
        matches = [
            record.resource_id
            for record in registry.list_resources(resource_type="lambda:function")
            if record.name == function_name
        ]
        if len(matches) == 1:
            return matches[0]
        built = _arn(event, "lambda", f"function:{function_name}")
        if built:
            return built
    queue_url = params.get("queueUrl")
    if queue_url:
        matches = [
            record.resource_id
            for record in registry.list_resources(resource_type="sqs:queue")
            if (record.config or {}).get("queue_url") == queue_url
        ]
        if len(matches) == 1:
            return matches[0]
    table_name = params.get("tableName")
    if table_name:
        built = _arn(event, "dynamodb", f"table/{table_name}")
        if built:
            return built
    items = ((params.get("instancesSet") or {}).get("items") or [])
    if items and items[0].get("instanceId"):
        instance_id = items[0]["instanceId"]
        matches = [
            record.resource_id
            for record in registry.list_resources(resource_type="ec2:instance")
            if (record.config or {}).get("instance_id") == instance_id
        ]
        if len(matches) == 1:
            return matches[0]
        built = _arn(event, "ec2", f"instance/{instance_id}")
        if built:
            return built
    return ""


def _arn(event: dict, service: str, resource: str) -> str:
    account = event.get("account") or ""
    region = event.get("region") or ""
    if not account or not region:
        return ""
    try:
        parse_arn(f"arn:aws:{service}:{region}:{account}:{resource}")
    except ValueError:
        return ""
    return f"arn:aws:{service}:{region}:{account}:{resource}"
