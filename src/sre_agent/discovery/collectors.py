"""Collectors for the first inventory: Lambda, SQS, and EC2, plus optional sources."""

from __future__ import annotations

from dataclasses import dataclass, field

from sre_agent.aws.errors import AccessDenied, CloudError, NotFound, PartialRead, SourceNotConfigured
from sre_agent.aws.paging import paginate, with_backoff
from sre_agent.models import Relationship, ResourceRecord

API_TYPES = {"lambda:function", "sqs:queue", "ec2:instance", "dynamodb:table", "apigateway:restapi"}


@dataclass
class CollectorResult:
    collector: str
    account_id: str
    region: str
    status: str
    scanned_at: str
    message: str = ""
    resources: list[ResourceRecord] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    authoritative_types: set[str] = field(default_factory=set)
    confirm_absence_types: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    relationship_source: str = ""


def collect_lambdas(reader, account_id: str, region: str, seen_at: str, page_size: int) -> CollectorResult:
    return _collect_listed(
        collector="lambda-api",
        source="lambda-api",
        resource_type="lambda:function",
        account_id=account_id,
        region=region,
        seen_at=seen_at,
        list_page=lambda token: reader.list_functions_page(account_id, region, token, page_size),
        load_detail=lambda item: reader.get_function(account_id, region, item["name"]),
        relationship_source="lambda-event-source-mapping",
    )


def collect_queues(reader, account_id: str, region: str, seen_at: str, page_size: int) -> CollectorResult:
    return _collect_listed(
        collector="sqs-api",
        source="sqs-api",
        resource_type="sqs:queue",
        account_id=account_id,
        region=region,
        seen_at=seen_at,
        list_page=lambda token: reader.list_queues_page(account_id, region, token, page_size),
        load_detail=lambda item: reader.get_queue_by_url(account_id, region, item["url"]),
        relationship_source="sqs-redrive-policy",
    )


def collect_tables(reader, account_id: str, region: str, seen_at: str, page_size: int) -> CollectorResult:
    return _collect_listed(
        collector="dynamodb-api",
        source="dynamodb-api",
        resource_type="dynamodb:table",
        account_id=account_id,
        region=region,
        seen_at=seen_at,
        list_page=lambda token: reader.list_tables_page(account_id, region, token, page_size),
        load_detail=lambda item: reader.describe_table(account_id, region, item["name"]),
        relationship_source="dynamodb-stream",
    )


def collect_rest_apis(reader, account_id: str, region: str, seen_at: str, page_size: int) -> CollectorResult:
    return _collect_listed(
        collector="apigateway-api",
        source="apigateway-api",
        resource_type="apigateway:restapi",
        account_id=account_id,
        region=region,
        seen_at=seen_at,
        list_page=lambda token: reader.list_rest_apis_page(account_id, region, token, page_size),
        load_detail=lambda item: reader.get_rest_api(account_id, region, item["name"]),
        relationship_source="apigateway-integration",
    )


def collect_instances(reader, account_id: str, region: str, seen_at: str, page_size: int) -> CollectorResult:
    def fetch(token):
        return with_backoff(lambda: reader.describe_instances_page(account_id, region, token, page_size))

    base = _base("ec2-api", account_id, region, seen_at)
    try:
        details = paginate(fetch)
    except AccessDenied as exc:
        base.status = "denied"
        base.message = str(exc)
        return base
    except CloudError as exc:
        base.status = "error"
        base.message = str(exc)
        return base
    base.resources = [record_from_detail(item, "ec2-api", seen_at) for item in details]
    base.status = "success"
    base.authoritative_types = {"ec2:instance"}
    base.confirm_absence_types = {"ec2:instance"}
    base.message = f"Discovered {len(base.resources)} EC2 instances."
    return base


def collect_explorer(reader, account_id: str, region: str, seen_at: str) -> CollectorResult:
    base = _base("resource-explorer", account_id, region, seen_at)
    try:
        details = with_backoff(lambda: reader.search_resources(account_id, region))
    except SourceNotConfigured as exc:
        base.status = "not-configured"
        base.message = str(exc)
        return base
    except AccessDenied as exc:
        base.status = "denied"
        base.message = str(exc)
        return base
    except PartialRead as exc:
        return _partial(base, exc, "resource-explorer", seen_at)
    except CloudError as exc:
        base.status = "error"
        base.message = str(exc)
        return base
    records = [record_from_detail(item, "resource-explorer", seen_at) for item in details]
    base.resources = records
    base.status = "success"
    extra = {record.resource_type for record in records if record.resource_type not in API_TYPES}
    base.authoritative_types = set(extra)
    base.confirm_absence_types = set(extra)
    base.message = f"Resource Explorer returned {len(records)} resources."
    return base


def collect_config(reader, account_id: str, region: str, seen_at: str) -> CollectorResult:
    base = _base("aws-config", account_id, region, seen_at)
    try:
        details = with_backoff(lambda: reader.list_config_resources(account_id, region))
    except SourceNotConfigured as exc:
        base.status = "not-configured"
        base.message = str(exc)
        return base
    except AccessDenied as exc:
        base.status = "denied"
        base.message = str(exc)
        return base
    except PartialRead as exc:
        return _partial(base, exc, "aws-config", seen_at)
    except CloudError as exc:
        base.status = "error"
        base.message = str(exc)
        return base
    base.resources = [record_from_detail(item, "aws-config", seen_at) for item in details]
    base.status = "success"
    base.message = f"AWS Config returned {len(base.resources)} recorded resources."
    return base


def record_from_detail(item: dict, source: str, seen_at: str) -> ResourceRecord:
    tags = {str(key): str(value) for key, value in dict(item.get("tags") or {}).items()}
    return ResourceRecord(
        resource_id=item["resource_id"],
        account_id=item["account_id"],
        region=item["region"],
        resource_type=item["resource_type"],
        name=item["name"],
        tags=tags,
        last_seen_at=seen_at,
        first_seen_at=seen_at,
        discovery_source=source,
        config=dict(item.get("config") or {}),
        presence="present",
    )


def relationships_for_detail(item: dict) -> tuple[str, list[Relationship]]:
    if item.get("resource_type") == "lambda:function":
        relationships = []
        for mapping in item.get("event_source_mappings") or []:
            target = mapping.get("event_source_arn")
            if not target:
                continue
            relationships.append(
                Relationship(
                    source_id=item["resource_id"],
                    target_id=target,
                    kind="consumes",
                    evidence_source="lambda-event-source-mapping",
                    evidence={
                        "mapping_uuid": mapping.get("uuid", ""),
                        "state": mapping.get("state", ""),
                        "batch_size": mapping.get("batch_size", 0),
                    },
                )
            )
        return "lambda-event-source-mapping", relationships
    if item.get("resource_type") == "sqs:queue":
        policy = (item.get("config") or {}).get("redrive_policy") or {}
        target = policy.get("deadLetterTargetArn") if isinstance(policy, dict) else None
        if not target:
            return "sqs-redrive-policy", []
        return "sqs-redrive-policy", [
            Relationship(
                source_id=item["resource_id"],
                target_id=target,
                kind="dead_letters_to",
                evidence_source="sqs-redrive-policy",
                evidence={"redrive_policy": policy},
            )
        ]
    if item.get("resource_type") == "apigateway:restapi":
        relationships = []
        for integration in (item.get("config") or {}).get("integrations") or []:
            target = integration.get("lambda_arn")
            if not target:
                continue
            relationships.append(
                Relationship(
                    source_id=item["resource_id"],
                    target_id=target,
                    kind="invokes",
                    evidence_source="apigateway-integration",
                    evidence={"method": integration.get("method", ""), "path": integration.get("path", "")},
                )
            )
        return "apigateway-integration", relationships
    return "", []


def _collect_listed(collector, source, resource_type, account_id, region, seen_at, list_page, load_detail, relationship_source) -> CollectorResult:
    base = _base(collector, account_id, region, seen_at)
    partial_note = ""
    try:
        summaries = paginate(lambda token: with_backoff(lambda: list_page(token)))
    except PartialRead as exc:
        summaries = list(exc.items)
        partial_note = str(exc)
    except AccessDenied as exc:
        base.status = "denied"
        base.message = str(exc)
        return base
    except CloudError as exc:
        base.status = "error"
        base.message = str(exc)
        return base

    errors: list[str] = []
    for summary in summaries:
        label = summary.get("name") or summary.get("url") or "resource"
        try:
            detail = with_backoff(lambda summary=summary: load_detail(summary))
        except NotFound:
            continue
        except AccessDenied as exc:
            errors.append(f"AccessDenied {label}: {exc}")
            continue
        except CloudError as exc:
            errors.append(f"{label}: {exc}")
            continue
        base.resources.append(record_from_detail(detail, source, seen_at))
        _, relationships = relationships_for_detail(detail)
        base.relationships.extend(relationships)

    base.errors = errors
    if errors and base.resources:
        base.status = "partial"
        base.authoritative_types = {resource_type}
        base.message = f"Partial {collector} scan: {len(base.resources)} read, {len(errors)} failed."
    elif errors:
        denied = all(item.startswith("AccessDenied") for item in errors)
        base.status = "denied" if denied else "error"
        base.message = "; ".join(errors)
    else:
        base.status = "success"
        base.authoritative_types = {resource_type}
        base.confirm_absence_types = {resource_type}
        base.message = f"Discovered {len(base.resources)} {resource_type} resources."
    if partial_note:
        base.status = "partial"
        base.confirm_absence_types = set()
        base.authoritative_types = {resource_type} if base.resources else set()
        base.message = f"Coverage is incomplete. {partial_note} Read {len(base.resources)} {resource_type} resources before the failure."
    base.relationship_source = relationship_source
    return base


def _partial(base: CollectorResult, exc: PartialRead, source: str, seen_at: str) -> CollectorResult:
    base.resources = [record_from_detail(item, source, seen_at) for item in exc.items if isinstance(item, dict) and item.get("resource_id")]
    base.status = "partial"
    base.confirm_absence_types = set()
    base.message = f"Coverage is incomplete. {exc}"
    return base


def _base(collector: str, account_id: str, region: str, seen_at: str) -> CollectorResult:
    return CollectorResult(
        collector=collector,
        account_id=account_id,
        region=region,
        status="error",
        scanned_at=seen_at,
    )
