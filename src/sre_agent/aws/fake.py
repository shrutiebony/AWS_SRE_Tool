"""In-memory cloud used by local runs, tests, and the lab.

Production monitoring never calls the lab_* methods. Those exist so the fault
injection runner can publish a recovery, or refuse to, after a repair API returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sre_agent.aws.errors import AccessDenied, CloudError, NotFound, SourceNotConfigured, Throttled
from sre_agent.aws.paging import Page


@dataclass
class FunctionRecord:
    name: str
    arn: str
    timeout: int = 30
    memory: int = 128
    runtime: str = "python3.12"
    last_modified: str = "2026-10-01T00:00:00+00:00"
    state: str = "Active"
    tags: dict[str, str] = field(default_factory=dict)
    mappings: list[dict] = field(default_factory=list)


@dataclass
class QueueRecord:
    name: str
    arn: str
    url: str
    tags: dict[str, str] = field(default_factory=dict)
    visibility_timeout: str = "30"
    redrive_policy: dict | None = None
    messages: int = 0
    message_records: list[dict] = field(default_factory=list)


@dataclass
class InstanceRecord:
    instance_id: str
    arn: str
    instance_type: str = "t3.micro"
    state: str = "running"
    tags: dict[str, str] = field(default_factory=dict)
    launch_time: str = "2026-10-01T00:00:00+00:00"
    status_check: str | None = "ok"


class FakeCloud:
    def __init__(self, account_id: str, region: str) -> None:
        self.account_id = account_id
        self.region = region
        self.functions: dict[str, FunctionRecord] = {}
        self.queues: dict[str, QueueRecord] = {}
        self.instances: dict[str, InstanceRecord] = {}
        self.metrics: dict[str, dict[str, list[tuple[datetime, float]]]] = {}
        self.logs: dict[str, list[dict[str, str]]] = {}
        self.move_tasks: dict[str, dict] = {}
        self.explorer_enabled = False
        self.config_enabled = False
        self.explorer_resources: list[dict] = []
        self.config_resources: list[dict] = []
        self.deny: set[str] = set()
        self.throttle_remaining: dict[str, int] = {}
        self.detail_errors: dict[str, Exception] = {}
        self.complete_moves_immediately = True
        self.calls: list[str] = []
        self.claimed_account = account_id
        self.orders: dict[str, dict] = {}
        self.tables: dict[str, dict] = {}
        self.rest_apis: dict[str, dict] = {}
        self.explorer_page_error = False

    def add_function(self, record: FunctionRecord) -> None:
        self.functions[record.name] = record

    def add_queue(self, record: QueueRecord) -> None:
        self.queues[record.name] = record

    def add_instance(self, record: InstanceRecord) -> None:
        self.instances[record.instance_id] = record

    def set_metric(self, resource_id: str, name: str, value: float, at: datetime) -> None:
        self.metrics.setdefault(resource_id, {}).setdefault(name, []).append((at, value))

    def set_logs(self, resource_id: str, lines: list[str], at: str) -> None:
        self.logs[resource_id] = [{"timestamp": at, "message": line} for line in lines]

    def lab_publish_metrics(self, resource_id: str, values: dict[str, float], at: datetime) -> None:
        """Lab-only: publish a fresh telemetry sample. Production code must not call this."""
        for name, value in values.items():
            self.set_metric(resource_id, name, value, at)

    def list_functions_page(self, account_id: str, region: str, token: str | None, page_size: int) -> Page:
        self._guard("list_functions", account_id, region)
        rows = [{"name": item.name, "arn": item.arn} for item in self.functions.values()]
        return _page(rows, token, page_size)

    def get_function(self, account_id: str, region: str, name: str) -> dict:
        self._guard("get_function", account_id, region)
        if name in self.detail_errors:
            raise self.detail_errors[name]
        record = self.functions.get(name)
        if record is None:
            raise NotFound(name)
        return {
            "resource_id": record.arn,
            "account_id": account_id,
            "region": region,
            "resource_type": "lambda:function",
            "name": record.name,
            "tags": dict(record.tags),
            "config": {
                "timeout": record.timeout,
                "memory": record.memory,
                "runtime": record.runtime,
                "last_modified": record.last_modified,
                "state": record.state,
            },
            "event_source_mappings": [dict(item) for item in record.mappings],
        }

    def list_queues_page(self, account_id: str, region: str, token: str | None, page_size: int) -> Page:
        self._guard("list_queues", account_id, region)
        rows = [{"name": item.name, "url": item.url, "arn": item.arn} for item in self.queues.values()]
        return _page(rows, token, page_size)

    def get_queue(self, account_id: str, region: str, name: str) -> dict:
        self._guard("get_queue", account_id, region)
        record = self.queues.get(name)
        if record is None:
            raise NotFound(name)
        return _queue_payload(record, account_id, region)

    def get_queue_by_url(self, account_id: str, region: str, url: str) -> dict:
        self._guard("get_queue", account_id, region)
        for record in self.queues.values():
            if record.url == url:
                return _queue_payload(record, account_id, region)
        raise NotFound(url)

    def describe_instances_page(self, account_id: str, region: str, token: str | None, page_size: int) -> Page:
        self._guard("describe_instances", account_id, region)
        rows = [_instance_payload(item, account_id, region) for item in self.instances.values()]
        return _page(rows, token, page_size)

    def get_metric_data(
        self,
        account_id: str,
        region: str,
        resource_id: str,
        metric_names: list[str],
        start: datetime,
        end: datetime,
        stat: str | None = None,
        period: int | None = None,
    ) -> dict[str, list[tuple[datetime, float]]]:
        del period
        self._guard("get_metrics", account_id, region)
        stored = self.metrics.get(resource_id, {})
        result = {}
        for name in metric_names:
            points = [(at, value) for at, value in stored.get(name, []) if start <= at <= end]
            if stat in {"Count", "SampleCount"}:
                points = [(at, 1.0) for at, _value in points]
            result[name] = points
        return result

    def filter_logs(self, account_id: str, region: str, resource_id: str, start: datetime, end: datetime) -> list[dict]:
        self._guard("filter_logs", account_id, region)
        return [dict(item) for item in self.logs.get(resource_id, [])]

    def lookup(self, account_id: str, region: str, resource_type: str, resource_id: str) -> dict:
        self._guard("lookup", account_id, region)
        if resource_type == "lambda:function":
            for record in self.functions.values():
                if record.arn == resource_id:
                    return self.get_function(account_id, region, record.name)
            raise NotFound(resource_id)
        if resource_type == "sqs:queue":
            for record in self.queues.values():
                if record.arn == resource_id:
                    return self.get_queue(account_id, region, record.name)
            raise NotFound(resource_id)
        if resource_type == "ec2:instance":
            for record in self.instances.values():
                if record.arn == resource_id:
                    return _instance_payload(record, account_id, region)
            raise NotFound(resource_id)
        if resource_type == "dynamodb:table":
            for table in self.tables.values():
                if table["resource_id"] == resource_id:
                    return dict(table)
            raise NotFound(resource_id)
        if resource_type == "apigateway:restapi":
            for api in self.rest_apis.values():
                if api["resource_id"] == resource_id:
                    return dict(api)
            raise NotFound(resource_id)
        raise NotFound(resource_id)

    def caller_account(self) -> str:
        return self.claimed_account

    def search_resources(self, account_id: str, region: str) -> list[dict]:
        from sre_agent.aws.pages import drain_pages

        self._guard("resource_explorer", account_id, region)
        if not self.explorer_enabled:
            raise SourceNotConfigured("Resource Explorer is not enabled in this scope.")

        def fetch(token):
            if self.explorer_page_error and token:
                raise CloudError("Resource Explorer next page failed")
            page = _page(self.explorer_resources, token, 1)
            return [dict(item) for item in page.items], page.next_token

        return drain_pages(fetch)

    def list_config_resources(self, account_id: str, region: str) -> list[dict]:
        self._guard("aws_config", account_id, region)
        if not self.config_enabled:
            raise SourceNotConfigured("AWS Config recording is not enabled for this scope.")
        return [dict(item) for item in self.config_resources]

    def list_message_move_tasks(self, account_id: str, region: str, dlq_arn: str) -> list[dict]:
        self._guard("list_move_tasks", account_id, region)
        return [dict(task) for task in self.move_tasks.values() if task["dlq_arn"] == dlq_arn]

    def start_message_move_task(self, account_id: str, region: str, source_arn: str, dlq_arn: str) -> dict:
        self._guard("start_move", account_id, region)
        self.calls.append("start_message_move_task")
        source = self._queue_by_arn(source_arn)
        dlq = self._queue_by_arn(dlq_arn)
        task_id = f"task-{len(self.move_tasks) + 1}"
        if self.complete_moves_immediately:
            moved = dlq.messages
            dlq.messages = 0
            source.messages += moved
            dlq.message_records = []
            status = "COMPLETED"
        else:
            moved = 0
            status = "RUNNING"
        task = {
            "task_id": task_id,
            "status": status,
            "source_arn": source_arn,
            "dlq_arn": dlq_arn,
            "moved": moved,
        }
        self.move_tasks[task_id] = task
        return dict(task)

    def replay_orders(self, account_id: str, region: str, source_arn: str, dlq_arn: str, order_ids: list[str]) -> dict:
        """Move only the listed orders. Unsampled and rejected messages stay on the queue."""
        self._guard("replay_orders", account_id, region)
        source = self._queue_by_arn(source_arn)
        dlq = self._queue_by_arn(dlq_arn)
        wanted = set(order_ids)
        task_id = f"task-{len(self.move_tasks) + 1}"
        if not self.complete_moves_immediately:
            task = {
                "task_id": task_id,
                "status": "RUNNING",
                "source_arn": source_arn,
                "dlq_arn": dlq_arn,
                "moved": 0,
                "order_ids": list(order_ids),
            }
            self.move_tasks[task_id] = task
            return dict(task)
        kept = []
        moved: list[str] = []
        for record in dlq.message_records:
            order_id = str(record.get("order_id") or "")
            if order_id and order_id in wanted:
                moved.append(order_id)
            else:
                kept.append(record)
        dlq.message_records = kept
        dlq.messages = max(0, dlq.messages - len(moved))
        source.messages += len(moved)
        task = {
            "task_id": task_id,
            "status": "COMPLETED",
            "source_arn": source_arn,
            "dlq_arn": dlq_arn,
            "moved": len(moved),
            "order_ids": moved,
        }
        self.move_tasks[task_id] = task
        return dict(task)

    def sample_dlq_messages(self, account_id: str, region: str, queue_arn: str) -> list[dict]:
        self._guard("sample_messages", account_id, region)
        return [dict(item) for item in self._queue_by_arn(queue_arn).message_records]

    def processed_order_ids(self, account_id: str, region: str, order_ids: list[str], not_before: str = "") -> set[str]:
        self._guard("processed_orders", account_id, region)
        done = set()
        for order_id in order_ids:
            item = self.orders.get(order_id) or {}
            processed_at = str(item.get("processed_at") or "")
            if item.get("status") == "processed" and processed_at > not_before:
                done.add(order_id)
        return done

    def lab_mark_processed(self, order_ids: list[str], at: str) -> None:
        for order_id in order_ids:
            self.orders[order_id] = {"status": "processed", "processed_at": at}

    def run_logs_query(self, account_id: str, region: str, resource_id: str, query: str, start: datetime, end: datetime) -> list[dict]:
        del start, end
        self._guard("logs_insights", account_id, region)
        rows = []
        for item in self.logs.get(resource_id, []):
            message = str(item.get("message", ""))
            if query and query.lower() in message.lower():
                rows.append(dict(item))
        return rows

    def list_tables_page(self, account_id: str, region: str, token: str | None, page_size: int) -> Page:
        self._guard("list_tables", account_id, region)
        names = sorted(self.tables)
        return _page([{"name": name} for name in names], token, page_size)

    def describe_table(self, account_id: str, region: str, name: str) -> dict:
        self._guard("describe_table", account_id, region)
        table = self.tables.get(name)
        if table is None:
            raise NotFound(name)
        return dict(table)

    def list_rest_apis_page(self, account_id: str, region: str, token: str | None, page_size: int) -> Page:
        self._guard("list_rest_apis", account_id, region)
        names = sorted(self.rest_apis)
        return _page([{"name": name} for name in names], token, page_size)

    def get_rest_api(self, account_id: str, region: str, name: str) -> dict:
        self._guard("get_rest_api", account_id, region)
        api = self.rest_apis.get(name)
        if api is None:
            raise NotFound(name)
        return dict(api)

    def cancel_message_move_task(self, account_id: str, region: str, task_id: str) -> dict:
        self._guard("cancel_move", account_id, region)
        self.calls.append("cancel_message_move_task")
        task = self.move_tasks.get(task_id)
        if task is None:
            raise NotFound(task_id)
        task["status"] = "CANCELLED"
        return dict(task)

    def _queue_by_arn(self, arn: str) -> QueueRecord:
        for record in self.queues.values():
            if record.arn == arn:
                return record
        raise NotFound(arn)

    def _guard(self, operation: str, account_id: str, region: str) -> None:
        self.calls.append(operation)
        if account_id != self.account_id or region != self.region:
            raise AccessDenied(f"{operation} is not allowed for {account_id} {region}")
        if operation in self.deny:
            raise AccessDenied(operation)
        remaining = self.throttle_remaining.get(operation, 0)
        if remaining > 0:
            self.throttle_remaining[operation] = remaining - 1
            raise Throttled(operation)


def _page(items: list, token: str | None, page_size: int) -> Page:
    offset = 0 if not token else int(token.split(":", 1)[1])
    chunk = items[offset : offset + page_size]
    next_offset = offset + page_size
    next_token = f"offset:{next_offset}" if next_offset < len(items) else None
    return Page(items=chunk, next_token=next_token)


def _queue_payload(record: QueueRecord, account_id: str, region: str) -> dict:
    return {
        "resource_id": record.arn,
        "account_id": account_id,
        "region": region,
        "resource_type": "sqs:queue",
        "name": record.name,
        "tags": dict(record.tags),
        "config": {
            "queue_url": record.url,
            "visibility_timeout": record.visibility_timeout,
            "redrive_policy": dict(record.redrive_policy) if record.redrive_policy else None,
            "approximate_messages": record.messages,
        },
    }


def _instance_payload(record: InstanceRecord, account_id: str, region: str) -> dict:
    return {
        "resource_id": record.arn,
        "account_id": account_id,
        "region": region,
        "resource_type": "ec2:instance",
        "name": record.tags.get("Name", record.instance_id),
        "tags": dict(record.tags),
        "config": {
            "instance_id": record.instance_id,
            "instance_type": record.instance_type,
            "state": record.state,
            "launch_time": record.launch_time,
            "status_check": record.status_check,
        },
    }
