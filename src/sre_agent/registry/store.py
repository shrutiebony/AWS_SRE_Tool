"""Resource registry.

The in-memory backend is the local and test store. DynamoDB is the deployed
store. Both go through the same upsert rules, so a scan cannot create a second
record for the same account, Region, type, and stable id.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sre_agent.models import (
    ActionRecord,
    Diagnosis,
    Escalation,
    Lesson,
    Outcome,
    PolicyDecision,
    Relationship,
    ResourceRecord,
    ScanAccess,
    VerificationResult,
)
from sre_agent.registry.codec import from_plain, to_plain


class RegistryTable(Protocol):
    def put_item(self, item: dict[str, Any]) -> None: ...

    def get_item(self, identity: str) -> dict[str, Any] | None: ...

    def query_collection(self, collection: str) -> list[dict[str, Any]]: ...

    def put_conditional(self, item: dict[str, Any], now_iso: str) -> bool: ...

    def release_conditional(self, identity: str, owner: str) -> bool: ...


class MemoryTable:
    """Table interface used in tests and by the local agent process."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def put_item(self, item: dict[str, Any]) -> None:
        self.items[item["identity"]] = json.loads(json.dumps(item))

    def get_item(self, identity: str) -> dict[str, Any] | None:
        item = self.items.get(identity)
        return json.loads(json.dumps(item)) if item else None

    def query_collection(self, collection: str) -> list[dict[str, Any]]:
        return [
            json.loads(json.dumps(item))
            for item in self.items.values()
            if item.get("collection") == collection
        ]

    def put_conditional(self, item: dict[str, Any], now_iso: str) -> bool:
        current = self.items.get(item["identity"])
        owner = "" if current is None else str(current.get("owner") or "")
        expires = "" if current is None else str(current.get("expires_at") or "")
        if owner and owner != item["owner"] and expires > now_iso:
            return False
        self.items[item["identity"]] = json.loads(json.dumps(item))
        return True

    def release_conditional(self, identity: str, owner: str) -> bool:
        current = self.items.get(identity)
        if current is None or current.get("owner") != owner:
            return False
        updated = json.loads(json.dumps(current))
        updated["owner"] = ""
        updated["expires_at"] = ""
        self.items[identity] = updated
        return True


class Boto3TableAdapter:
    """Adapt a boto3 DynamoDB Table resource to RegistryTable."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def put_item(self, item: dict[str, Any]) -> None:
        self.table.put_item(Item=item)

    def get_item(self, identity: str) -> dict[str, Any] | None:
        response = self.table.get_item(Key={"identity": identity})
        return response.get("Item")

    def query_collection(self, collection: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        items: list[dict[str, Any]] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {
                "IndexName": "collection-index",
                "KeyConditionExpression": Key("collection").eq(collection),
            }
            if token:
                kwargs["ExclusiveStartKey"] = token
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))
            token = response.get("LastEvaluatedKey")
            if not token:
                return items

    def put_conditional(self, item: dict[str, Any], now_iso: str) -> bool:
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#owner) OR #owner = :empty OR #owner = :owner OR #expires <= :now",
                ExpressionAttributeNames={"#owner": "owner", "#expires": "expires_at"},
                ExpressionAttributeValues={":empty": "", ":owner": item["owner"], ":now": now_iso},
            )
        except Exception as exc:
            if _conditional_failure(exc):
                return False
            raise
        return True

    def release_conditional(self, identity: str, owner: str) -> bool:
        try:
            self.table.update_item(
                Key={"identity": identity},
                ConditionExpression="#owner = :owner",
                UpdateExpression="SET #owner = :empty, expires_at = :empty_time",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":owner": owner, ":empty": "", ":empty_time": ""},
            )
        except Exception as exc:
            if _conditional_failure(exc):
                return False
            raise
        return True


class DocumentBackend:
    def __init__(self, table: RegistryTable) -> None:
        self.table = table

    def save(self, collection: str, key: str, value: Any) -> None:
        self.table.put_item(
            {
                "identity": f"{collection}#{key}",
                "collection": collection,
                "doc": json.dumps(to_plain(value)),
            }
        )

    def load(self, collection: str, key: str) -> Any | None:
        item = self.table.get_item(f"{collection}#{key}")
        if not item:
            return None
        return from_plain(json.loads(item["doc"]))

    def load_all(self, collection: str) -> list[Any]:
        rows = []
        for item in self.table.query_collection(collection):
            rows.append(from_plain(json.loads(item["doc"])))
        return rows


class Registry:
    def __init__(self, table: RegistryTable | None = None) -> None:
        self.backend = DocumentBackend(table or MemoryTable())

    def upsert(self, incoming: ResourceRecord, mode: str) -> ResourceRecord:
        existing = self.backend.load("resources", incoming.identity)
        merged = _merge(existing, incoming, mode)
        self.backend.save("resources", merged.identity, merged)
        return merged

    def save(self, record: ResourceRecord) -> ResourceRecord:
        self.backend.save("resources", record.identity, record)
        return record

    def get_by_resource_id(self, resource_id: str) -> ResourceRecord | None:
        matches = [record for record in self.list_resources() if record.resource_id == resource_id]
        if not matches:
            return None
        if len(matches) > 1:
            raise RuntimeError(f"more than one registry record for {resource_id}")
        return matches[0]

    def list_resources(
        self,
        account_id: str | None = None,
        region: str | None = None,
        resource_type: str | None = None,
    ) -> list[ResourceRecord]:
        rows = []
        for record in self.backend.load_all("resources"):
            if account_id and record.account_id != account_id:
                continue
            if region and record.region != region:
                continue
            if resource_type and record.resource_type != resource_type:
                continue
            rows.append(record)
        return rows

    def mark_missing(self, account_id: str, region: str, resource_type: str, seen_ids: set[str]) -> list[str]:
        """A complete scan that does not see a resource makes it uncertain, not deleted."""
        changed: list[str] = []
        for record in self.list_resources(account_id, region, resource_type):
            if record.resource_id in seen_ids or record.presence == "deleted":
                continue
            record.miss_count += 1
            if record.presence == "present":
                record.presence = "uncertain"
                record.health = "unknown"
                changed.append(record.resource_id)
            self.save(record)
        return changed

    def mark_deleted(self, resource_id: str) -> ResourceRecord | None:
        record = self.get_by_resource_id(resource_id)
        if record is None:
            return None
        record.presence = "deleted"
        record.health = "unknown"
        record.health_reasons = ["A direct lookup confirmed that this resource is gone."]
        self.save(record)
        return record

    def replace_relationships(
        self,
        evidence_source: str,
        origin_ids: set[str],
        relationships: list[Relationship],
    ) -> None:
        kept = [
            item
            for item in self.relationships()
            if not (item.evidence_source == evidence_source and item.source_id in origin_ids)
        ]
        for item in self.relationships():
            self.backend.table.put_item(
                {
                    "identity": f"relationships#{item.identity}",
                    "collection": "deleted",
                    "doc": "{}",
                }
            )
        for item in kept + relationships:
            self.backend.save("relationships", item.identity, item)

    def relationships(self) -> list[Relationship]:
        return self.backend.load_all("relationships")

    def dependencies(self, resource_id: str) -> list[Relationship]:
        return [
            item
            for item in self.relationships()
            if item.source_id == resource_id or item.target_id == resource_id
        ]

    def add_scan(self, scan: ScanAccess) -> None:
        key = f"{scan.scanned_at}|{scan.account_id}|{scan.region}|{scan.collector}|{scan.status}"
        self.backend.save("scans", key, scan)

    def scans(self) -> list[ScanAccess]:
        return self.backend.load_all("scans")

    def add_incident(self, diagnosis: Diagnosis) -> None:
        self.backend.save("incidents", diagnosis.incident_id, diagnosis)

    def incidents(self) -> list[Diagnosis]:
        return self.backend.load_all("incidents")

    def add_decision(self, decision: PolicyDecision) -> None:
        self.backend.save("decisions", decision.incident_id, decision)

    def decisions(self) -> list[PolicyDecision]:
        return self.backend.load_all("decisions")

    def add_action(self, action: ActionRecord) -> None:
        self.backend.save("actions", action.action_id, action)

    def actions(self) -> list[ActionRecord]:
        return self.backend.load_all("actions")

    def add_verification(self, result: VerificationResult) -> None:
        self.backend.save("verifications", result.action_id, result)

    def verifications(self) -> list[VerificationResult]:
        return self.backend.load_all("verifications")

    def add_escalation(self, escalation: Escalation) -> None:
        if any(
            item.resource_id == escalation.resource_id and item.reason == escalation.reason
            for item in self.escalations()
        ):
            return
        self.backend.save("escalations", f"{escalation.incident_id}|{escalation.reason}", escalation)

    def escalations(self) -> list[Escalation]:
        return self.backend.load_all("escalations")

    def add_outcome(self, outcome: Outcome) -> None:
        self.backend.save("outcomes", outcome.incident_id, outcome)

    def outcomes(self) -> list[Outcome]:
        return self.backend.load_all("outcomes")

    def add_lesson(self, lesson: Lesson) -> None:
        key = f"{lesson.created_at}|{lesson.resource_id}|{lesson.symptom_key}|{lesson.runbook_id}"
        self.backend.save("lessons", key, lesson)

    def lessons_for(self, symptom_key: str) -> list[Lesson]:
        return [item for item in self.backend.load_all("lessons") if item.symptom_key == symptom_key]

    def block_runbook(self, resource_id: str, runbook_id: str, reason: str) -> None:
        record = self.get_by_resource_id(resource_id)
        if record is None:
            return
        record.runbook_blocks[runbook_id] = reason
        self.save(record)

    def note_change(self, resource_id: str, event_name: str, summary: str, at: str) -> None:
        record = self.get_by_resource_id(resource_id)
        if record is None:
            return
        record.recent_changes.append({"at": at, "event_name": event_name, "summary": summary})
        record.recent_changes = record.recent_changes[-20:]
        self.save(record)

    def associate_alarm(self, alarm_name: str, resource_id: str) -> None:
        self.backend.save("alarm_links", alarm_name, {"alarm_name": alarm_name, "resource_id": resource_id})

    def resource_for_alarm(self, alarm_name: str) -> str:
        item = self.backend.load("alarm_links", alarm_name)
        if not item:
            return ""
        return str(item.get("resource_id", ""))

    def acquire_lock(self, resource_id: str, runbook_id: str, owner: str, now_iso: str, ttl_seconds: int) -> bool:
        from datetime import timedelta

        from sre_agent.clock import parse_time

        expires = (parse_time(now_iso) + timedelta(seconds=ttl_seconds)).isoformat()
        identity = f"locks#{resource_id}|{runbook_id}"
        return self.backend.table.put_conditional(
            {
                "identity": identity,
                "collection": "locks",
                "owner": owner,
                "expires_at": expires,
                "resource_id": resource_id,
                "runbook_id": runbook_id,
            },
            now_iso,
        )

    def release_lock(self, resource_id: str, runbook_id: str, owner: str) -> None:
        identity = f"locks#{resource_id}|{runbook_id}"
        self.backend.table.release_conditional(identity, owner)


def _conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _merge(existing: ResourceRecord | None, incoming: ResourceRecord, mode: str) -> ResourceRecord:
    if mode not in {"authoritative", "hint", "terraform", "sns"}:
        raise ValueError(f"unknown upsert mode {mode}")
    if existing is None:
        created = _copy(incoming)
        created.first_seen_at = created.first_seen_at or created.last_seen_at
        created.sources = _append_source(created.sources, created.discovery_source)
        if mode != "authoritative":
            created.presence = "uncertain"
            created.health = "unknown"
        return created

    merged = _copy(existing)
    if mode == "authoritative":
        merged.name = incoming.name
        merged.tags = dict(incoming.tags)
        merged.config = dict(incoming.config)
        merged.last_seen_at = incoming.last_seen_at
        merged.discovery_source = incoming.discovery_source
        merged.presence = "present"
        merged.miss_count = 0
        merged.sources = _append_source(merged.sources, incoming.discovery_source)
        return merged
    if mode == "terraform":
        merged.intended_config = dict(incoming.intended_config)
        merged.ownership = {**merged.ownership, **incoming.ownership}
        merged.managed_by = incoming.managed_by or merged.managed_by
        merged.sources = _append_source(merged.sources, incoming.discovery_source)
        return merged
    if mode == "sns":
        merged.metadata = {**merged.metadata, **incoming.metadata}
        tags = dict(merged.tags)
        for key, value in incoming.tags.items():
            tags.setdefault(key, value)
        merged.tags = tags
        merged.sources = _append_source(merged.sources, incoming.discovery_source)
        return merged
    merged.sources = _append_source(merged.sources, incoming.discovery_source)
    config = dict(merged.config)
    for key, value in incoming.config.items():
        config.setdefault(key, value)
    merged.config = config
    return merged


def _append_source(sources: list[str], source: str) -> list[str]:
    if source and source not in sources:
        return [*sources, source]
    return list(sources)


def _copy(record: ResourceRecord) -> ResourceRecord:
    return from_plain(to_plain(record))
