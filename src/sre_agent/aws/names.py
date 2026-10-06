"""Parse ARNs and walk Terraform state or an SNS registration message."""

from __future__ import annotations

import json
from typing import Any

from sre_agent.models import ResourceRecord

SERVICE_TYPES = {
    "lambda": "lambda:function",
    "sqs": "sqs:queue",
    "ec2": "ec2:instance",
    "s3": "s3:bucket",
    "dynamodb": "dynamodb:table",
    "apigateway": "apigateway:restapi",
}

TERRAFORM_TYPES = {
    "aws_lambda_function": "lambda:function",
    "aws_sqs_queue": "sqs:queue",
    "aws_instance": "ec2:instance",
}


def parse_arn(arn: str) -> dict[str, str]:
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        raise ValueError(f"not an ARN: {arn}")
    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account": parts[4],
        "resource": parts[5],
    }


def resource_name(parsed: dict[str, str]) -> str:
    resource = parsed["resource"]
    if parsed["service"] == "lambda" and resource.startswith("function:"):
        return resource.split(":", 1)[1].split(":")[0]
    if "/" in resource:
        return resource.rsplit("/", 1)[-1]
    return resource


def metric_identity(resource_id: str) -> tuple[str, list[dict[str, str]]]:
    parsed = parse_arn(resource_id)
    name = resource_name(parsed)
    if parsed["service"] == "lambda":
        return "AWS/Lambda", [{"Name": "FunctionName", "Value": name}]
    if parsed["service"] == "sqs":
        return "AWS/SQS", [{"Name": "QueueName", "Value": name}]
    if parsed["service"] == "ec2":
        return "AWS/EC2", [{"Name": "InstanceId", "Value": name}]
    if parsed["service"] == "dynamodb":
        return "AWS/DynamoDB", [{"Name": "TableName", "Value": name}]
    if parsed["service"] == "apigateway":
        return "AWS/ApiGateway", [{"Name": "ApiName", "Value": name}]
    raise ValueError(f"no metric mapping for {resource_id}")


def log_group_for(resource_id: str) -> str | None:
    parsed = parse_arn(resource_id)
    name = resource_name(parsed)
    if parsed["service"] == "lambda":
        return f"/aws/lambda/{name}"
    if parsed["service"] == "ec2":
        return f"/aws/ec2/{name}"
    return None


def load_state_document(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def resources_from_terraform(document: dict[str, Any], seen_at: str, default_account: str, default_region: str) -> tuple[list[ResourceRecord], list[str]]:
    errors: list[str] = []
    records: list[ResourceRecord] = []
    for resource in _iter_state_resources(document):
        resource_type = TERRAFORM_TYPES.get(resource.get("type", ""))
        if resource_type is None:
            continue
        attributes = _attributes(resource)
        if not attributes:
            errors.append(f"skipped {resource.get('type')} {resource.get('name')} with no attributes")
            continue
        try:
            record = _record_from_attributes(
                resource_type,
                resource,
                attributes,
                seen_at,
                default_account,
                default_region,
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        records.append(record)
    return records, errors


def record_from_sns(message: dict[str, Any], seen_at: str) -> ResourceRecord:
    resource_id = str(message.get("resource_id") or "")
    parsed = parse_arn(resource_id)
    resource_type = str(message.get("resource_type") or SERVICE_TYPES.get(parsed["service"], f"{parsed['service']}:resource"))
    region = parsed["region"] or str(message.get("region") or "global")
    metadata = {
        key: str(message[key])
        for key in ("owner", "team", "notes")
        if message.get(key)
    }
    tags = {str(key): str(value) for key, value in dict(message.get("tags") or {}).items()}
    return ResourceRecord(
        resource_id=resource_id,
        account_id=parsed["account"] or str(message.get("account_id") or ""),
        region=region,
        resource_type=resource_type,
        name=str(message.get("name") or resource_name(parsed)),
        tags=tags,
        last_seen_at=seen_at,
        discovery_source="sns",
        metadata=metadata,
        presence="uncertain",
    )


def _iter_state_resources(document: dict[str, Any]):
    resources = document.get("resources")
    if isinstance(resources, list):
        yield from resources
    root = document.get("values", {}).get("root_module", {})
    if isinstance(root, dict):
        yield from _walk_module(root)


def _walk_module(module: dict[str, Any]):
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from _walk_module(child)


def _attributes(resource: dict[str, Any]) -> dict[str, Any]:
    values = resource.get("values")
    if isinstance(values, dict):
        return values
    instances = resource.get("instances") or []
    if instances and isinstance(instances[0], dict):
        attributes = instances[0].get("attributes") or {}
        if isinstance(attributes, dict):
            return attributes
    return {}


def _record_from_attributes(resource_type, resource, attributes, seen_at, default_account, default_region) -> ResourceRecord:
    arn = attributes.get("arn") or ""
    region = default_region
    account_id = default_account
    if arn:
        parsed = parse_arn(str(arn))
        region = parsed["region"] or region
        account_id = parsed["account"] or account_id
        resource_id = str(arn)
        name = resource_name(parsed)
    elif resource_type == "ec2:instance" and attributes.get("id"):
        instance_id = str(attributes["id"])
        zone = str(attributes.get("availability_zone") or "")
        if zone:
            region = zone[:-1]
        resource_id = f"arn:aws:ec2:{region}:{account_id}:instance/{instance_id}"
        name = instance_id
    else:
        raise ValueError(f"skipped {resource.get('type')} {resource.get('name')} without a stable id")
    if attributes.get("function_name"):
        name = str(attributes["function_name"])
    elif attributes.get("name") and resource_type == "sqs:queue":
        name = str(attributes["name"])
    tags = {str(key): str(value) for key, value in dict(attributes.get("tags") or {}).items()}
    ownership = {
        "terraform_address": f"{resource.get('module', '')}.{resource.get('type')}.{resource.get('name')}".lstrip("."),
        "terraform_type": str(resource.get("type")),
    }
    if tags.get("owner"):
        ownership["owner"] = tags["owner"]
    if tags.get("team"):
        ownership["team"] = tags["team"]
    intended = {
        key: attributes[key]
        for key in ("timeout", "memory_size", "runtime", "visibility_timeout_seconds", "instance_type")
        if key in attributes
    }
    return ResourceRecord(
        resource_id=resource_id,
        account_id=account_id,
        region=region,
        resource_type=resource_type,
        name=name,
        tags=tags,
        last_seen_at=seen_at,
        discovery_source="terraform-state",
        intended_config=intended,
        ownership=ownership,
        managed_by="terraform",
        presence="uncertain",
    )
