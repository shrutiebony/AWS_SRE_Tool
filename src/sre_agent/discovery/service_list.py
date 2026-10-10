"""Which extra resource types to inventory. The list is data, not code.

A Confluence heading such as ``Service: EKS (eks:cluster)`` is enough. When
``SRE_SERVICE_LIST_URI`` points at an S3 object or a local file, only the
resource types in that file are added. The original five collectors still run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class ServiceListError(Exception):
    pass


@dataclass(frozen=True)
class WatchedService:
    service: str
    resource_type: str


def load_service_list(uri: str | None = None) -> list[WatchedService]:
    if uri is None:
        from sre_agent.local_config import get

        location = get("SRE_SERVICE_LIST_URI")
    else:
        location = uri.strip()
    if not location:
        return []
    return parse_service_list(_read(location))


def parse_service_list(raw: str) -> list[WatchedService]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceListError(f"The service list is not JSON. {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("services", [])
    if not isinstance(payload, list):
        raise ServiceListError("The service list must be a JSON array.")
    services = []
    for item in payload:
        if isinstance(item, str) and ":" in item:
            services.append(WatchedService(item, item))
            continue
        if not isinstance(item, dict):
            raise ServiceListError("Each service must be a resource type or an object with resource_type.")
        resource_type = str(item.get("resource_type") or "").strip()
        service = str(item.get("service") or resource_type).strip()
        if not resource_type or ":" not in resource_type:
            raise ServiceListError(f"{service or 'A service'} needs a resource_type such as eks:cluster.")
        services.append(WatchedService(service, resource_type))
    return services


def service_type_map(services: list[WatchedService]) -> dict[str, str]:
    return {item.service.strip().lower(): item.resource_type for item in services if item.service.strip()}


def types_to_discover(rules, services: list[WatchedService]) -> list[WatchedService]:
    """Extra types beyond the built-in collectors. An S3 list restricts that set."""
    from sre_agent.discovery.collectors import API_TYPES

    found: dict[str, WatchedService] = {}
    for rule in rules:
        resource_type = getattr(rule, "resource_type", "")
        if not resource_type or resource_type in API_TYPES:
            continue
        found[resource_type] = WatchedService(resource_type, resource_type)
    if not services:
        return list(found.values())
    allowed = {item.resource_type for item in services}
    selected = [item for item in found.values() if item.resource_type in allowed]
    seen = {item.resource_type for item in selected}
    for item in services:
        if item.resource_type in API_TYPES or item.resource_type in seen:
            continue
        selected.append(item)
        seen.add(item.resource_type)
    return selected


def _read(location: str) -> str:
    if location.startswith("s3://"):
        return _read_s3(location)
    try:
        with open(location, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise ServiceListError(f"The service list could not be read. {exc}") from exc


def _read_s3(uri: str) -> str:
    rest = uri[5:]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ServiceListError("SRE_SERVICE_LIST_URI must look like s3://bucket/key.")
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:
        raise ServiceListError(f"The service list could not be read from {uri}. {exc}") from exc
    return response["Body"].read().decode()
