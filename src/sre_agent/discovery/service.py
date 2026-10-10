"""Discovery and reconciliation. This replaces a mandatory publisher.

A denied or unconfigured source is recorded. It is never treated as an empty account.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sre_agent.aws.errors import AccessDenied, NotFound
from sre_agent.aws.names import SERVICE_TYPES, parse_arn, record_from_sns, resources_from_terraform
from sre_agent.clock import Clock
from sre_agent.discovery.collectors import (
    CollectorResult,
    collect_config,
    collect_explorer,
    collect_instances,
    collect_lambdas,
    collect_queues,
    collect_rest_apis,
    collect_tables,
    collect_watched,
    record_from_detail,
    relationships_for_detail,
)
from sre_agent.aws.parallel import parallel_map
from sre_agent.models import ScanAccess
from sre_agent.registry.store import Registry

ABSENCE = "This does not mean the resources do not exist."


@dataclass
class AccountTarget:
    account_id: str
    regions: list[str]
    reader: object | None = None
    terraform_state: dict | None = None
    enable_resource_explorer: bool = False
    enable_config: bool = False


@dataclass
class Scope:
    accounts: list[AccountTarget] = field(default_factory=list)
    page_size: int = 100


class DiscoveryReconciliationService:
    def __init__(self, scope: Scope, registry: Registry, clock: Clock) -> None:
        self.scope = scope
        self.registry = registry
        self.clock = clock
        self.watched: list = []

    def run(self) -> list[CollectorResult]:
        return self.finish(self.scan())

    def finish(self, results: list[CollectorResult]) -> list[CollectorResult]:
        self.apply(results)
        status = "partial" if any(item.status == "error" for item in results) else "success"
        self.registry.add_scan(
            ScanAccess(
                account_id="*",
                region="*",
                collector="reconciliation",
                status=status,
                scanned_at=self.clock.iso(),
                resource_count=len(self.registry.list_resources()),
                message="Reconciliation applied the collector results.",
            )
        )
        return results

    def scan(self) -> list[CollectorResult]:
        results: list[CollectorResult] = []
        for account in self.scope.accounts:
            if account.reader is None:
                results.append(
                    self._note(
                        CollectorResult(
                            collector="account-access",
                            account_id=account.account_id,
                            region="*",
                            status="denied",
                            scanned_at=self.clock.iso(),
                            message=f"No credentials for account {account.account_id}.",
                        )
                    )
                )
                continue
            identity = self._caller_account(account)
            if identity and identity != account.account_id:
                results.append(
                    self._note(
                        CollectorResult(
                            collector="account-access",
                            account_id=account.account_id,
                            region="*",
                            status="denied",
                            scanned_at=self.clock.iso(),
                            message=f"Caller identity is {identity}, not the configured account {account.account_id}.",
                        )
                    )
                )
                continue
            results.append(self._terraform(account))
            for region in account.regions:
                results.extend(self._region_collectors(account, region))
            results.append(
                self._note(
                    CollectorResult(
                        collector="global-services",
                        account_id=account.account_id,
                        region="global",
                        status="not-configured",
                        scanned_at=self.clock.iso(),
                        message="Global services are not scanned in this scope.",
                    )
                )
            )
        return results

    def scan_watched(self, blocked: set[str] | None = None) -> list[CollectorResult]:
        """Extra service-list types. Accounts already denied are left alone."""
        skipped = blocked or set()
        results: list[CollectorResult] = []
        if not self.watched:
            return results
        for account in self.scope.accounts:
            if account.reader is None or account.account_id in skipped:
                continue
            for region in account.regions:
                seen_at = self.clock.iso()
                jobs = [
                    (
                        lambda watched=watched, seen_at=seen_at, account=account, region=region: self._note(
                            collect_watched(
                                account.reader,
                                account.account_id,
                                region,
                                seen_at,
                                watched.resource_type,
                            )
                        )
                    )
                    for watched in self.watched
                ]
                results.extend(parallel_map(lambda fn: fn(), jobs))
        return results

    def _region_collectors(self, account: AccountTarget, region: str) -> list[CollectorResult]:
        seen_at = self.clock.iso()
        page_size = self.scope.page_size
        jobs = [
            lambda: self._explorer(account, region),
            lambda: self._config(account, region),
            lambda: self._note(collect_lambdas(account.reader, account.account_id, region, seen_at, page_size)),
            lambda: self._note(collect_queues(account.reader, account.account_id, region, seen_at, page_size)),
            lambda: self._note(collect_instances(account.reader, account.account_id, region, seen_at, page_size)),
            lambda: self._note(collect_tables(account.reader, account.account_id, region, seen_at, page_size)),
            lambda: self._note(collect_rest_apis(account.reader, account.account_id, region, seen_at, page_size)),
        ]
        for watched in self.watched:
            jobs.append(
                lambda watched=watched: self._note(
                    collect_watched(account.reader, account.account_id, region, seen_at, watched.resource_type)
                )
            )
        return parallel_map(lambda fn: fn(), jobs)

    def apply(self, results: list[CollectorResult]) -> None:
        for result in results:
            self.registry.add_scan(
                ScanAccess(
                    account_id=result.account_id,
                    region=result.region,
                    collector=result.collector,
                    status=result.status,
                    scanned_at=result.scanned_at,
                    resource_count=len(result.resources),
                    message=result.message,
                )
            )
            if result.status in {"denied", "error", "not-configured"}:
                continue
            for record in result.resources:
                mode = "authoritative" if record.resource_type in result.authoritative_types else _hint_mode(result)
                self.registry.upsert(record, mode)
            if result.relationship_source and result.status in {"success", "partial"}:
                origins = {record.resource_id for record in result.resources}
                self.registry.replace_relationships(result.relationship_source, origins, result.relationships)
            if result.status == "success":
                for resource_type in result.confirm_absence_types:
                    seen = {
                        record.resource_id
                        for record in result.resources
                        if record.resource_type == resource_type
                    }
                    self.registry.mark_missing(result.account_id, result.region, resource_type, seen)

    def refresh_one(self, resource_id: str) -> str:
        record = self.registry.get_by_resource_id(resource_id)
        if record is None:
            return self._discover_new(resource_id)
        reader = _reader_for(self.scope, record.account_id)
        if reader is None:
            self.registry.add_scan(
                self._scan(record.account_id, record.region, "lookup", "denied", f"No credentials for {record.account_id}.")
            )
            return "denied"
        try:
            detail = reader.lookup(record.account_id, record.region, record.resource_type, record.resource_id)
        except NotFound:
            self.registry.mark_deleted(resource_id)
            self.registry.add_scan(
                self._scan(record.account_id, record.region, "lookup", "success", "Direct lookup confirmed deletion.")
            )
            return "deleted"
        except AccessDenied as exc:
            self.registry.add_scan(self._scan(record.account_id, record.region, "lookup", "denied", str(exc)))
            return "denied"
        refreshed = record_from_detail(detail, _source_for(record.resource_type), self.clock.iso())
        self.registry.upsert(refreshed, "authoritative")
        source, relationships = relationships_for_detail(detail)
        if source:
            self.registry.replace_relationships(source, {resource_id}, relationships)
        return "present"

    def ingest_sns(self, message: dict) -> ScanAccess:
        try:
            record = record_from_sns(message, self.clock.iso())
        except ValueError as exc:
            scan = self._scan("*", "*", "sns", "error", str(exc))
            self.registry.add_scan(scan)
            return scan
        self.registry.upsert(record, "sns")
        scan = self._scan(
            record.account_id,
            record.region,
            "sns",
            "success",
            "SNS registration merged as optional metadata.",
            resource_count=1,
        )
        self.registry.add_scan(scan)
        return scan

    def _terraform(self, account: AccountTarget) -> CollectorResult:
        if account.terraform_state is None:
            return self._note(
                CollectorResult(
                    collector="terraform-state",
                    account_id=account.account_id,
                    region="*",
                    status="not-configured",
                    scanned_at=self.clock.iso(),
                    message="No Terraform state was provided.",
                )
            )
        region = account.regions[0] if account.regions else "*"
        records, errors = resources_from_terraform(
            account.terraform_state,
            self.clock.iso(),
            account.account_id,
            region,
        )
        return self._note(
            CollectorResult(
                collector="terraform-state",
                account_id=account.account_id,
                region="*",
                status="success" if not errors else "partial",
                scanned_at=self.clock.iso(),
                message="Terraform state adds intended configuration and ownership. It is not a complete inventory."
                + (f" Skipped: {'; '.join(errors)}." if errors else ""),
                resources=records,
            )
        )

    def _explorer(self, account: AccountTarget, region: str) -> CollectorResult:
        if not account.enable_resource_explorer:
            return self._note(
                CollectorResult(
                    collector="resource-explorer",
                    account_id=account.account_id,
                    region=region,
                    status="not-configured",
                    scanned_at=self.clock.iso(),
                    message="Resource Explorer was not requested for this scope.",
                )
            )
        return self._note(collect_explorer(account.reader, account.account_id, region, self.clock.iso()))

    def _config(self, account: AccountTarget, region: str) -> CollectorResult:
        if not account.enable_config:
            return self._note(
                CollectorResult(
                    collector="aws-config",
                    account_id=account.account_id,
                    region=region,
                    status="not-configured",
                    scanned_at=self.clock.iso(),
                    message="AWS Config recording was not requested for this scope.",
                )
            )
        return self._note(collect_config(account.reader, account.account_id, region, self.clock.iso()))

    def _note(self, result: CollectorResult) -> CollectorResult:
        if result.status in {"denied", "not-configured", "error"} and ABSENCE not in result.message:
            result.message = f"{result.message} {ABSENCE}".strip()
        return result

    def _caller_account(self, account: AccountTarget) -> str:
        reader = account.reader
        if reader is None or not hasattr(reader, "caller_account"):
            return account.account_id
        try:
            return str(reader.caller_account())
        except Exception as exc:
            self.registry.add_scan(self._scan(account.account_id, "*", "account-access", "denied", str(exc)))
            return account.account_id

    def _discover_new(self, resource_id: str) -> str:
        try:
            parsed = parse_arn(resource_id)
        except ValueError:
            return "unknown"
        resource_type = SERVICE_TYPES.get(parsed["service"], "")
        account_id = parsed["account"]
        region = parsed["region"]
        if not resource_type or not account_id or not region:
            return "unknown"
        reader = _reader_for(self.scope, account_id)
        if reader is None:
            self.registry.add_scan(self._scan(account_id, region, "lookup", "denied", f"No credentials for {account_id}."))
            return "denied"
        try:
            detail = reader.lookup(account_id, region, resource_type, resource_id)
        except NotFound:
            self.registry.add_scan(
                self._scan(account_id, region, "lookup", "error", "A new event named a resource the service API does not have yet.")
            )
            return "absent"
        except AccessDenied as exc:
            self.registry.add_scan(self._scan(account_id, region, "lookup", "denied", str(exc)))
            return "denied"
        refreshed = record_from_detail(detail, _source_for(resource_type), self.clock.iso())
        self.registry.upsert(refreshed, "authoritative")
        source, relationships = relationships_for_detail(detail)
        if source:
            self.registry.replace_relationships(source, {resource_id}, relationships)
        return "present"

    def _scan(self, account_id: str, region: str, collector: str, status: str, message: str, resource_count: int = 0) -> ScanAccess:
        if status in {"denied", "not-configured", "error"} and ABSENCE not in message:
            message = f"{message} {ABSENCE}".strip()
        return ScanAccess(
            account_id=account_id,
            region=region,
            collector=collector,
            status=status,
            scanned_at=self.clock.iso(),
            resource_count=resource_count,
            message=message,
        )


def _hint_mode(result: CollectorResult) -> str:
    if result.collector == "terraform-state":
        return "terraform"
    if result.collector == "sns":
        return "sns"
    return "hint"


def _source_for(resource_type: str) -> str:
    return {
        "lambda:function": "lambda-api",
        "sqs:queue": "sqs-api",
        "ec2:instance": "ec2-api",
        "dynamodb:table": "dynamodb-api",
        "apigateway:restapi": "apigateway-api",
    }.get(resource_type, "lookup")


def _reader_for(scope: Scope, account_id: str):
    for account in scope.accounts:
        if account.account_id == account_id:
            return account.reader
    return None
