"""Read-only boto3 adapter. Collectors see the same normalized records as the fake cloud."""

from __future__ import annotations

import json
import threading
from datetime import datetime

from sre_agent.aws.errors import NotFound, SourceNotConfigured, map_exception
from sre_agent.aws.names import log_group_for, metric_identity, parse_arn, resource_name
from sre_agent.aws.paging import Page

COUNTERS = {
    "Errors",
    "Invocations",
    "Throttles",
    "5XXError",
    "Count",
    "ReadThrottleEvents",
    "WriteThrottleEvents",
    "UserErrors",
}


class Boto3Reader:
    def __init__(
        self,
        session=None,
        role_arn: str | None = None,
        region: str | None = None,
        explorer_view_arn: str | None = None,
        order_table: str | None = None,
    ) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("boto3 is required for live AWS reads. Install the aws extra.") from exc
        self._boto3 = boto3
        self._base_session = session or boto3.Session(region_name=region)
        self.role_arn = role_arn
        self.region = region
        self._expires = None
        self.session = self._base_session
        self._client_lock = threading.RLock()
        self._clients: dict[tuple[str, str], object] = {}
        self._config = Config(
            retries={"total_max_attempts": 2, "mode": "standard"},
            connect_timeout=3,
            read_timeout=12,
            max_pool_connections=32,
        )
        if role_arn:
            self._refresh_credentials(force=True)
        self.explorer_view_arn = explorer_view_arn
        self.order_table = order_table

    def client(self, service: str, region: str):
        self._refresh_credentials()
        key = (service, region)
        with self._client_lock:
            cached = self._clients.get(key)
            if cached is None:
                cached = self.session.client(service, region_name=region, config=self._config)
                self._clients[key] = cached
            return cached

    def list_functions_page(self, account_id: str, region: str, token: str | None, page_size: int) -> Page:
        del account_id
        kwargs: dict = {"MaxItems": page_size}
        if token:
            kwargs["Marker"] = token
        response = self._invoke(lambda: self.client("lambda", region).list_functions(**kwargs))
        items = [
            {"name": item["FunctionName"], "arn": item["FunctionArn"]}
            for item in response.get("Functions", [])
        ]
        return Page(items, response.get("NextMarker"))

    def get_function(self, account_id: str, region: str, name: str) -> dict:
        client = self.client("lambda", region)

        def configuration():
            return self._invoke(lambda: client.get_function(FunctionName=name))
        def mappings_for() -> list[dict]:
            found: list[dict] = []
            marker = None
            while True:
                kwargs = {"FunctionName": name}
                if marker:
                    kwargs["Marker"] = marker
                page = self._invoke(lambda kwargs=kwargs: client.list_event_source_mappings(**kwargs))
                for item in page.get("EventSourceMappings", []):
                    found.append(
                        {
                            "uuid": item.get("UUID", ""),
                            "event_source_arn": item.get("EventSourceArn", ""),
                            "state": item.get("State", ""),
                            "batch_size": item.get("BatchSize", 0),
                        }
                    )
                marker = page.get("NextMarker")
                if not marker:
                    return found

        from sre_agent.aws.parallel import parallel_map

        response, mappings = parallel_map(lambda fn: fn(), [configuration, mappings_for])
        configuration = response["Configuration"]
        return {
            "resource_id": configuration["FunctionArn"],
            "account_id": account_id,
            "region": region,
            "resource_type": "lambda:function",
            "name": configuration["FunctionName"],
            "tags": {str(key): str(value) for key, value in dict(response.get("Tags") or {}).items()},
            "config": {
                "timeout": configuration.get("Timeout"),
                "memory": configuration.get("MemorySize"),
                "runtime": configuration.get("Runtime"),
                "last_modified": configuration.get("LastModified"),
                "state": configuration.get("State"),
            },
            "event_source_mappings": mappings,
        }

    def list_queues_page(self, account_id: str, region: str, token: str | None, page_size: int) -> Page:
        del account_id
        kwargs: dict = {"MaxResults": page_size}
        if token:
            kwargs["NextToken"] = token
        response = self._invoke(lambda: self.client("sqs", region).list_queues(**kwargs))
        items = []
        for url in response.get("QueueUrls", []):
            items.append({"name": url.rstrip("/").split("/")[-1], "url": url})
        return Page(items, response.get("NextToken"))

    def get_queue(self, account_id: str, region: str, name: str) -> dict:
        client = self.client("sqs", region)
        url_response = self._invoke(lambda: client.get_queue_url(QueueName=name))
        return self.get_queue_by_url(account_id, region, url_response["QueueUrl"])

    def get_queue_by_url(self, account_id: str, region: str, url: str) -> dict:
        client = self.client("sqs", region)
        wanted = ["QueueArn", "VisibilityTimeout", "RedrivePolicy", "ApproximateNumberOfMessages"]

        def attributes_for():
            return self._invoke(lambda: client.get_queue_attributes(QueueUrl=url, AttributeNames=wanted))["Attributes"]

        def tags_for():
            try:
                return self._invoke(lambda: client.list_queue_tags(QueueUrl=url)).get("Tags", {})
            except NotFound:
                return {}

        from sre_agent.aws.parallel import parallel_map

        attributes, tags = parallel_map(lambda fn: fn(), [attributes_for, tags_for])
        policy = attributes.get("RedrivePolicy")
        return {
            "resource_id": attributes["QueueArn"],
            "account_id": account_id,
            "region": region,
            "resource_type": "sqs:queue",
            "name": attributes.get("QueueName") or url.rstrip("/").split("/")[-1],
            "tags": {str(key): str(value) for key, value in dict(tags).items()},
            "config": {
                "queue_url": url,
                "visibility_timeout": attributes.get("VisibilityTimeout"),
                "redrive_policy": json.loads(policy) if policy else None,
                "approximate_messages": int(attributes.get("ApproximateNumberOfMessages", "0")),
            },
        }

    def describe_instances_page(self, account_id: str, region: str, token: str | None, page_size: int) -> Page:
        kwargs: dict = {"MaxResults": page_size}
        if token:
            kwargs["NextToken"] = token
        response = self._invoke(lambda: self.client("ec2", region).describe_instances(**kwargs))
        instances = []
        for reservation in response.get("Reservations", []):
            instances.extend(reservation.get("Instances", []))
        statuses = self._instance_statuses(region, [item["InstanceId"] for item in instances])
        items = [_normalize_instance(item, statuses.get(item["InstanceId"]), account_id, region) for item in instances]
        return Page(items, response.get("NextToken"))

    def get_metric_data(
        self,
        account_id,
        region,
        resource_id,
        metric_names,
        start: datetime,
        end: datetime,
        stat: str | None = None,
        period: int | None = None,
        namespace: str | None = None,
        dimensions: list | None = None,
    ):
        del account_id
        if namespace is None or dimensions is None:
            try:
                mapped_namespace, mapped_dimensions = metric_identity(resource_id)
            except ValueError:
                mapped_namespace, mapped_dimensions = None, None
            namespace = namespace or mapped_namespace
            if dimensions is None:
                dimensions = mapped_dimensions
        if not namespace or not dimensions:
            return {name: [] for name in metric_names}
        queries = [
            {
                "id": f"m{index}",
                "namespace": namespace,
                "metric_name": name,
                "dimensions": dimensions,
                "period": period or 60,
                "stat": _cloudwatch_stat(stat, name),
            }
            for index, name in enumerate(metric_names)
        ]
        found = self.query_metrics(region, queries, start, end)
        return {name: found.get(f"m{index}", []) for index, name in enumerate(metric_names)}

    def query_metrics(self, region: str, queries: list[dict], start: datetime, end: datetime) -> dict[str, list]:
        """One GetMetricData for many resources. Each query needs id, namespace, metric_name, dimensions, period, and stat."""
        if not queries:
            return {}
        found: dict[str, list] = {str(item["id"]): [] for item in queries}
        for offset in range(0, len(queries), 100):
            chunk = queries[offset : offset + 100]
            payload = [
                {
                    "Id": str(item["id"]),
                    "MetricStat": {
                        "Metric": {
                            "Namespace": item["namespace"],
                            "MetricName": item["metric_name"],
                            "Dimensions": item["dimensions"],
                        },
                        "Period": int(item.get("period") or 60),
                        "Stat": item["stat"],
                    },
                    "ReturnData": True,
                }
                for item in chunk
            ]
            token = None
            while True:
                kwargs = {"MetricDataQueries": payload, "StartTime": start, "EndTime": end}
                if token:
                    kwargs["NextToken"] = token
                response = self._invoke(lambda kwargs=kwargs: self.client("cloudwatch", region).get_metric_data(**kwargs))
                for item in response.get("MetricDataResults", []):
                    points = found.setdefault(item["Id"], [])
                    points.extend(
                        (stamp, float(value))
                        for stamp, value in zip(item.get("Timestamps", []), item.get("Values", []))
                    )
                token = response.get("NextToken")
                if not token:
                    break
        return found

    def filter_logs(self, account_id, region, resource_id, start: datetime, end: datetime) -> list[dict]:
        del account_id
        group = log_group_for(resource_id)
        if group is None:
            return []
        from sre_agent.aws.pages import drain_pages

        def fetch(token):
            kwargs = {
                "logGroupName": group,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": 100,
            }
            if token:
                kwargs["nextToken"] = token
            try:
                response = self._invoke(lambda kwargs=kwargs: self.client("logs", region).filter_log_events(**kwargs))
            except NotFound:
                return [], None
            events = [
                {"timestamp": str(item.get("timestamp", "")), "message": item.get("message", "")}
                for item in response.get("events", [])
            ]
            return events, response.get("nextToken")

        return drain_pages(fetch)

    def lookup(self, account_id: str, region: str, resource_type: str, resource_id: str) -> dict:
        parsed = parse_arn(resource_id)
        name = resource_name(parsed)
        if resource_type == "lambda:function":
            return self.get_function(account_id, region, name)
        if resource_type == "sqs:queue":
            return self.get_queue(account_id, region, name)
        if resource_type == "ec2:instance":
            response = self._invoke(lambda: self.client("ec2", region).describe_instances(InstanceIds=[name]))
            instances = [
                instance
                for reservation in response.get("Reservations", [])
                for instance in reservation.get("Instances", [])
            ]
            if not instances:
                raise NotFound(resource_id)
            statuses = self._instance_statuses(region, [name])
            return _normalize_instance(instances[0], statuses.get(name), account_id, region)
        if resource_type == "dynamodb:table":
            return self.describe_table(account_id, region, name)
        if resource_type == "apigateway:restapi":
            return self.get_rest_api(account_id, region, name)
        raise NotFound(resource_id)

    def caller_account(self) -> str:
        from sre_agent.local_config import get

        region = self.region or get("SRE_REGION") or get("AWS_REGION")
        response = self._invoke(lambda: self.client("sts", region).get_caller_identity())
        return response["Account"]

    def list_tagged_resources(self, account_id: str, region: str, resource_type: str) -> list[dict]:
        del account_id
        client = self.client("resourcegroupstaggingapi", region)
        items = []
        token = None
        while True:
            kwargs = {"ResourceTypeFilters": [resource_type], "ResourcesPerPage": 100}
            if token:
                kwargs["PaginationToken"] = token
            response = self._invoke(lambda kwargs=kwargs: client.get_resources(**kwargs))
            for mapping in response.get("ResourceTagMappingList", []):
                arn = str(mapping.get("ResourceARN") or "")
                if not arn:
                    continue
                name = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
                items.append({"resource_id": arn, "name": name, "resource_type": resource_type})
            token = response.get("PaginationToken") or ""
            if not token:
                break
        return items

    def search_resources(self, account_id: str, region: str) -> list[dict]:
        from sre_agent.aws.pages import drain_pages

        del account_id
        if not self.explorer_view_arn:
            raise SourceNotConfigured("Resource Explorer view ARN is not configured.")

        def fetch(token):
            kwargs = {"QueryString": "*", "MaxResults": 100, "ViewArn": self.explorer_view_arn}
            if token:
                kwargs["NextToken"] = token
            response = self._invoke(lambda kwargs=kwargs: self.client("resource-explorer-2", region).search(**kwargs))
            items = []
            for item in response.get("Resources", []):
                arn = item.get("Arn", "")
                try:
                    parsed = parse_arn(arn)
                except ValueError:
                    continue
                items.append(
                    {
                        "resource_id": arn,
                        "account_id": parsed["account"],
                        "region": parsed["region"] or region,
                        "resource_type": item.get("ResourceType") or f"{parsed['service']}:resource",
                        "name": resource_name(parsed),
                        "tags": {},
                        "config": {"explorer_properties": item.get("Properties") or []},
                    }
                )
            return items, response.get("NextToken")

        return drain_pages(fetch)

    def list_config_resources(self, account_id: str, region: str) -> list[dict]:
        from sre_agent.aws.pages import drain_pages

        types = (
            "AWS::Lambda::Function",
            "AWS::SQS::Queue",
            "AWS::EC2::Instance",
            "AWS::DynamoDB::Table",
            "AWS::ApiGateway::RestApi",
        )
        items = []
        for resource_type in types:
            def fetch(token, resource_type=resource_type):
                kwargs = {"resourceType": resource_type}
                if token:
                    kwargs["nextToken"] = token
                response = self._invoke(
                    lambda kwargs=kwargs: self.client("config", region).list_discovered_resources(**kwargs)
                )
                page = [
                    {
                        "resource_id": item.get("resourceId", ""),
                        "account_id": account_id,
                        "region": region,
                        "resource_type": item.get("resourceType", resource_type),
                        "name": item.get("resourceName") or item.get("resourceId", ""),
                        "tags": {},
                        "config": {},
                    }
                    for item in response.get("resourceIdentifiers", [])
                ]
                return page, response.get("nextToken")

            items.extend(drain_pages(fetch))
        return items

    def list_message_move_tasks(self, account_id: str, region: str, dlq_arn: str) -> list[dict]:
        del account_id
        response = self._invoke(lambda: self.client("sqs", region).list_message_move_tasks(SourceArn=dlq_arn))
        return [
            {
                "task_id": item.get("TaskHandle", ""),
                "status": item.get("Status", ""),
                "source_arn": item.get("DestinationArn", ""),
                "dlq_arn": dlq_arn,
            }
            for item in response.get("Results", [])
        ]

    def replay_orders(self, account_id: str, region: str, source_arn: str, dlq_arn: str, order_ids: list[str]) -> dict:
        """Replay listed orders only. A bulk move is not used, so unsampled messages stay put."""
        del account_id
        wanted = set(order_ids)
        source_url = self._queue_url(region, source_arn)
        dlq_url = self._queue_url(region, dlq_arn)
        attrs = self._invoke(
            lambda: self.client("sqs", region).get_queue_attributes(
                QueueUrl=dlq_url, AttributeNames=["ApproximateNumberOfMessages"]
            )
        )
        visible = int((attrs.get("Attributes") or {}).get("ApproximateNumberOfMessages") or 0)
        moved: list[str] = []
        seen = 0
        while seen < max(visible, 1) and seen < 100:
            response = self._invoke(
                lambda: self.client("sqs", region).receive_message(
                    QueueUrl=dlq_url,
                    MaxNumberOfMessages=10,
                    VisibilityTimeout=30,
                    MessageAttributeNames=["All"],
                    WaitTimeSeconds=0,
                )
            )
            messages = response.get("Messages") or []
            if not messages:
                break
            for item in messages:
                seen += 1
                body = item.get("Body", "")
                attributes = item.get("MessageAttributes") or {}
                order_id = (attributes.get("order_id") or {}).get("StringValue") or _order_id_from_body(body)
                receipt = item.get("ReceiptHandle", "")
                if order_id and order_id in wanted:
                    self._invoke(
                        lambda body=body, order_id=order_id: self.client("sqs", region).send_message(
                            QueueUrl=source_url,
                            MessageBody=body,
                            MessageAttributes={
                                "order_id": {"DataType": "String", "StringValue": order_id},
                            },
                        )
                    )
                    self._invoke(
                        lambda receipt=receipt: self.client("sqs", region).delete_message(
                            QueueUrl=dlq_url, ReceiptHandle=receipt
                        )
                    )
                    if order_id not in moved:
                        moved.append(order_id)
                elif receipt:
                    self._invoke(
                        lambda receipt=receipt: self.client("sqs", region).change_message_visibility(
                            QueueUrl=dlq_url, ReceiptHandle=receipt, VisibilityTimeout=0
                        )
                    )
            if seen >= visible:
                break
        return {
            "task_id": "selective",
            "status": "COMPLETED",
            "source_arn": source_arn,
            "dlq_arn": dlq_arn,
            "moved": len(moved),
            "order_ids": moved,
            "sampled": seen,
            "visible": visible,
        }

    def _queue_url(self, region: str, queue_arn: str) -> str:
        parsed = parse_arn(queue_arn)
        return self._invoke(
            lambda: self.client("sqs", region).get_queue_url(
                QueueName=resource_name(parsed),
                QueueOwnerAWSAccountId=parsed["account"],
            )
        )["QueueUrl"]

    def start_message_move_task(self, account_id: str, region: str, source_arn: str, dlq_arn: str) -> dict:
        del account_id
        response = self._invoke(
            lambda: self.client("sqs", region).start_message_move_task(
                SourceArn=dlq_arn,
                DestinationArn=source_arn,
            )
        )
        return {
            "task_id": response.get("TaskHandle", ""),
            "status": "RUNNING",
            "source_arn": source_arn,
            "dlq_arn": dlq_arn,
        }

    def cancel_message_move_task(self, account_id: str, region: str, task_id: str) -> dict:
        del account_id
        self._invoke(lambda: self.client("sqs", region).cancel_message_move_task(TaskHandle=task_id))
        return {"task_id": task_id, "status": "CANCELLED"}

    def list_tables_page(self, account_id: str, region: str, token: str | None, page_size: int):
        del account_id
        from sre_agent.aws.paging import Page

        kwargs = {"Limit": page_size}
        if token:
            kwargs["ExclusiveStartTableName"] = token
        response = self._invoke(lambda: self.client("dynamodb", region).list_tables(**kwargs))
        names = response.get("TableNames", [])
        return Page(items=[{"name": name} for name in names], next_token=response.get("LastEvaluatedTableName"))

    def describe_table(self, account_id: str, region: str, name: str) -> dict:
        response = self._invoke(lambda: self.client("dynamodb", region).describe_table(TableName=name))
        table = response["Table"]
        return {
            "resource_id": table.get("TableArn", f"arn:aws:dynamodb:{region}:{account_id}:table/{name}"),
            "account_id": account_id,
            "region": region,
            "resource_type": "dynamodb:table",
            "name": name,
            "tags": {},
            "config": {"billing_mode": table.get("BillingModeSummary", {}).get("BillingMode", "")},
        }

    def list_rest_apis_page(self, account_id: str, region: str, token: str | None, page_size: int):
        del account_id
        from sre_agent.aws.paging import Page

        kwargs = {"limit": page_size}
        if token:
            kwargs["position"] = token
        response = self._invoke(lambda: self.client("apigateway", region).get_rest_apis(**kwargs))
        items = [{"name": item["id"], "api_name": item.get("name", item["id"])} for item in response.get("items", [])]
        return Page(items=items, next_token=response.get("position"))

    def get_rest_api(self, account_id: str, region: str, name: str) -> dict:
        from sre_agent.aws.parallel import parallel_map

        def api_for():
            return self._invoke(lambda: self.client("apigateway", region).get_rest_api(restApiId=name))

        def resources_for():
            return self._invoke(lambda: self.client("apigateway", region).get_resources(restApiId=name, limit=500))

        def stages_for() -> list[str]:
            try:
                return self.rest_api_stages(region, name)
            except Exception:
                return []

        api, resources, stages = parallel_map(lambda fn: fn(), [api_for, resources_for, stages_for])
        integrations = []
        pairs = [
            (item, method)
            for item in resources.get("items", [])
            for method in (item.get("resourceMethods") or {})
        ]

        def fetch(pair: tuple[dict, str]) -> tuple[dict, str, dict]:
            item, method = pair
            detail = self._invoke(
                lambda item=item, method=method: self.client("apigateway", region).get_method(
                    restApiId=name, resourceId=item["id"], httpMethod=method
                )
            )
            return item, method, detail

        for item, method, detail in (fetch(pair) for pair in pairs):
            uri = (detail.get("methodIntegration") or {}).get("uri", "")
            marker = "/functions/"
            if marker in uri:
                lambda_arn = uri.split(marker, 1)[1].split("/invocations", 1)[0]
                integrations.append({"method": method, "path": item.get("path", ""), "lambda_arn": lambda_arn})
        return {
            "resource_id": f"arn:aws:apigateway:{region}::/restapis/{name}",
            "account_id": account_id,
            "region": region,
            "resource_type": "apigateway:restapi",
            "name": api.get("name") or name,
            "tags": api.get("tags") or {},
            "config": {"api_id": name, "stages": stages, "integrations": integrations},
        }

    def rest_api_stages(self, region: str, api_id: str) -> list[str]:
        response = self._invoke(lambda: self.client("apigateway", region).get_stages(restApiId=api_id))
        return [str(item.get("stageName")) for item in response.get("item", []) if item.get("stageName")]

    def run_logs_query(self, account_id, region, resource_id, query, start: datetime, end: datetime, log_group: str | None = None) -> list[dict]:
        from sre_agent.aws.errors import CloudError

        del account_id
        group = log_group or log_group_for(resource_id)
        if group is None:
            return []
        started = self._invoke(
            lambda: self.client("logs", region).start_query(
                logGroupName=group,
                startTime=int(start.timestamp()),
                endTime=int(end.timestamp()),
                queryString=query,
            )
        )
        import time

        status = ""
        response = {}
        for _ in range(5):
            response = self._invoke(lambda: self.client("logs", region).get_query_results(queryId=started["queryId"]))
            status = response.get("status", "")
            if status in {"Complete", "Failed", "Cancelled", "Timeout"}:
                break
            time.sleep(0.2)
        if status != "Complete":
            raise CloudError(f"Logs Insights query ended {status or 'incomplete'}")
        rows = []
        for row in response.get("results", []):
            rows.append({cell.get("field"): cell.get("value") for cell in row})
        return rows

    def sample_dlq_messages(self, account_id: str, region: str, queue_arn: str) -> list[dict]:
        parsed = parse_arn(queue_arn)
        url = self._invoke(
            lambda: self.client("sqs", region).get_queue_url(QueueName=resource_name(parsed), QueueOwnerAWSAccountId=account_id)
        )["QueueUrl"]
        response = self._invoke(
            lambda: self.client("sqs", region).receive_message(
                QueueUrl=url,
                MaxNumberOfMessages=10,
                VisibilityTimeout=0,
                MessageAttributeNames=["All"],
                WaitTimeSeconds=0,
            )
        )
        samples = []
        for item in response.get("Messages", []):
            attributes = item.get("MessageAttributes") or {}
            body = item.get("Body", "")
            order_id = (attributes.get("order_id") or {}).get("StringValue") or _order_id_from_body(body)
            samples.append(
                {
                    "id": item.get("MessageId", ""),
                    "order_id": order_id,
                    "body": body[:500],
                }
            )
        return samples

    def processed_order_ids(self, account_id: str, region: str, order_ids: list[str], not_before: str = "") -> set[str]:
        del account_id, region
        if not self.order_table:
            return set()
        table = self._boto3.resource("dynamodb").Table(self.order_table)
        done = set()
        for order_id in order_ids:
            item = self._invoke(
                lambda order_id=order_id: table.get_item(Key={"order_id": order_id}, ConsistentRead=True)
            ).get("Item") or {}
            processed_at = str(item.get("processed_at") or "")
            if str(item.get("status", "")) == "processed" and processed_at > not_before:
                done.add(order_id)
        return done

    def _refresh_credentials(self, force: bool = False) -> None:
        from datetime import datetime, timedelta, timezone

        if not self.role_arn:
            return
        if not force and self._expires is not None and self._expires - datetime.now(timezone.utc) > timedelta(minutes=5):
            return
        session, expires = _assumed_session(self._boto3, self._base_session, self.role_arn, self.region)
        with self._client_lock:
            self.session = session
            self._expires = expires
            self._clients.clear()

    def _instance_statuses(self, region: str, instance_ids: list[str]) -> dict[str, str]:
        if not instance_ids:
            return {}
        response = self._invoke(
            lambda: self.client("ec2", region).describe_instance_status(
                InstanceIds=instance_ids,
                IncludeAllInstances=True,
            )
        )
        statuses = {}
        for item in response.get("InstanceStatuses", []):
            instance_status = item.get("InstanceStatus", {}).get("Status")
            system_status = item.get("SystemStatus", {}).get("Status")
            if instance_status == "ok" and system_status == "ok":
                statuses[item["InstanceId"]] = "ok"
            elif instance_status or system_status:
                statuses[item["InstanceId"]] = "impaired"
        return statuses

    def _invoke(self, fn):
        try:
            return fn()
        except Exception as exc:
            mapped = map_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise


def metric_key(resource_id: str, name: str, stat: str | None, period: int | None, namespace: str | None, dimensions: list | None) -> tuple:
    """Identity of one CloudWatch series, matching get_metric_data's defaults."""
    if not namespace or not dimensions:
        try:
            mapped_namespace, mapped_dimensions = metric_identity(resource_id)
        except ValueError:
            mapped_namespace, mapped_dimensions = "", []
        if not namespace:
            namespace = mapped_namespace
        if not dimensions:
            dimensions = mapped_dimensions
    dims = tuple((str(item.get("Name")), str(item.get("Value"))) for item in (dimensions or []))
    return (resource_id, name, _cloudwatch_stat(stat, name), int(period or 60), namespace or "", dims)


def _cloudwatch_stat(stat: str | None, metric_name: str) -> str:
    if stat in {"Count", "SampleCount"}:
        return "SampleCount"
    if stat in {"Sum", "Average", "Maximum", "Minimum"}:
        return stat
    return "Sum" if metric_name in COUNTERS else "Maximum"


def _order_id_from_body(body: str) -> str:
    import json

    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return ""
    if isinstance(payload, dict):
        return str(payload.get("order_id") or "")
    return ""


def _assumed_session(boto3, session, role_arn: str, region: str | None):
    from datetime import timezone

    credentials = session.client("sts").assume_role(RoleArn=role_arn, RoleSessionName="sre-agent")["Credentials"]
    assumed = boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )
    expires = credentials["Expiration"]
    if getattr(expires, "tzinfo", None) is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return assumed, expires


def _normalize_instance(instance: dict, status: str | None, account_id: str, region: str) -> dict:
    instance_id = instance["InstanceId"]
    tags = {item.get("Key", ""): item.get("Value", "") for item in instance.get("Tags", [])}
    name = tags.get("Name") or instance_id
    launch = instance.get("LaunchTime")
    return {
        "resource_id": f"arn:aws:ec2:{region}:{account_id}:instance/{instance_id}",
        "account_id": account_id,
        "region": region,
        "resource_type": "ec2:instance",
        "name": name,
        "tags": tags,
        "config": {
            "instance_id": instance_id,
            "instance_type": instance.get("InstanceType"),
            "state": instance.get("State", {}).get("Name"),
            "launch_time": launch.isoformat() if hasattr(launch, "isoformat") else str(launch or ""),
            "status_check": status,
        },
    }

