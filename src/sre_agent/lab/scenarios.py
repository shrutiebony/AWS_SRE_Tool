"""Fixture accounts. These resources already exist; the agent does not create them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sre_agent.aws.fake import FakeCloud, FunctionRecord, InstanceRecord, QueueRecord
from sre_agent.clock import Clock
from sre_agent.discovery.service import AccountTarget, Scope

ACCOUNT = "123456789012"
REGION = "us-east-1"
OTHER_ACCOUNT = "210987654321"


def lambda_arn(name: str, account: str = ACCOUNT, region: str = REGION) -> str:
    return f"arn:aws:lambda:{region}:{account}:function:{name}"


def queue_arn(name: str, account: str = ACCOUNT, region: str = REGION) -> str:
    return f"arn:aws:sqs:{region}:{account}:{name}"


def queue_url(name: str, account: str = ACCOUNT, region: str = REGION) -> str:
    return f"https://sqs.{region}.amazonaws.com/{account}/{name}"


def instance_arn(instance_id: str, account: str = ACCOUNT, region: str = REGION) -> str:
    return f"arn:aws:ec2:{region}:{account}:instance/{instance_id}"


@dataclass
class World:
    cloud: FakeCloud
    scope: Scope
    clock: Clock
    consumer_id: str
    analytics_id: str
    retired_id: str
    queue_id: str
    dlq_id: str
    instance_id: str
    bucket_id: str


def build_world(*, poison: bool = False, consumer_unhealthy: bool = False, explorer: bool = True, terraform: bool = True) -> World:
    clock = Clock()
    cloud = FakeCloud(ACCOUNT, REGION)
    cloud.explorer_enabled = explorer
    consumer_id = lambda_arn("payment-worker")
    analytics_id = lambda_arn("analytics")
    retired_id = lambda_arn("retired-worker")
    queue_id = queue_arn("payments")
    dlq_id = queue_arn("payments-dlq")
    instance_id = instance_arn("i-0web")
    bucket_id = "arn:aws:s3:::payments-archive"
    cloud.add_function(
        FunctionRecord(
            name="payment-worker",
            arn=consumer_id,
            timeout=30,
            tags={"environment": "production", "owner": "payments"},
            last_modified="2026-10-01T00:00:00+00:00",
            mappings=[{"uuid": "esm-1", "event_source_arn": queue_id, "state": "Enabled", "batch_size": 10}],
        )
    )
    cloud.add_function(
        FunctionRecord(
            name="analytics",
            arn=analytics_id,
            timeout=15,
            tags={"environment": "production"},
        )
    )
    cloud.add_queue(
        QueueRecord(
            name="payments",
            arn=queue_id,
            url=queue_url("payments"),
            tags={"environment": "production"},
            redrive_policy={"deadLetterTargetArn": dlq_id, "maxReceiveCount": "5"},
            messages=20,
        )
    )
    cloud.add_queue(
        QueueRecord(
            name="payments-dlq",
            arn=dlq_id,
            url=queue_url("payments-dlq"),
            tags={"environment": "production"},
            messages=2,
            message_records=[
                {"id": "msg-1", "order_id": "order-1"},
                {"id": "msg-2", "order_id": "order-2"},
            ],
        )
    )
    cloud.add_instance(
        InstanceRecord(
            instance_id="i-0web",
            arn=instance_id,
            tags={"Name": "web", "environment": "production"},
            status_check="ok",
        )
    )
    now = clock.now()
    earlier = now - timedelta(minutes=10)
    _lambda_metrics(cloud, consumer_id, now, unhealthy=consumer_unhealthy)
    cloud.set_metric(queue_id, "ApproximateAgeOfOldestMessage", 900, now)
    cloud.set_metric(queue_id, "ApproximateNumberOfMessagesVisible", 20, now)
    cloud.set_metric(dlq_id, "ApproximateNumberOfMessagesVisible", 2, earlier)
    cloud.set_metric(dlq_id, "ApproximateNumberOfMessagesVisible", 8, now)
    cloud.set_metric(dlq_id, "ApproximateAgeOfOldestMessage", 120, now)
    cloud.set_metric(instance_id, "StatusCheckFailed", 0, now)
    cloud.set_metric(instance_id, "CPUUtilization", 12, now)
    if poison:
        cloud.set_logs(
            consumer_id,
            [
                '{"event":"permanent_failure","order_id":"order-1","detail":"schema validation failed"}',
                '{"event":"permanent_failure","order_id":"order-2","detail":"schema validation failed"}',
            ],
            clock.iso(),
        )
    else:
        cloud.set_logs(
            consumer_id,
            [
                '{"event":"transient_failure","order_id":"order-1","detail":"Task timed out after 30.00 seconds"}',
                '{"event":"transient_failure","order_id":"order-2","detail":"Connection reset by peer"}',
            ],
            clock.iso(),
        )
    if explorer:
        cloud.explorer_resources.append(
            {
                "resource_id": bucket_id,
                "account_id": ACCOUNT,
                "region": REGION,
                "resource_type": "s3:bucket",
                "name": "payments-archive",
                "tags": {"environment": "production"},
                "config": {},
            }
        )
    state = _state(consumer_id, queue_id, retired_id) if terraform else None
    scope = Scope(
        accounts=[
            AccountTarget(
                account_id=ACCOUNT,
                regions=[REGION],
                reader=cloud,
                terraform_state=state,
                enable_resource_explorer=explorer,
                enable_config=False,
            )
        ],
        page_size=10,
    )
    return World(cloud, scope, clock, consumer_id, analytics_id, retired_id, queue_id, dlq_id, instance_id, bucket_id)


def publish_recovery(world: World) -> None:
    """Lab-only observation after the verification window. Production code does not call this."""
    if not isinstance(world.cloud, FakeCloud):
        raise RuntimeError("Fault injection is a lab tool and cannot run against live AWS.")
    later = world.clock.now() + timedelta(seconds=60)
    world.cloud.lab_publish_metrics(
        world.queue_id,
        {"ApproximateAgeOfOldestMessage": 5, "ApproximateNumberOfMessagesVisible": 0},
        later,
    )
    world.cloud.lab_publish_metrics(world.dlq_id, {"ApproximateNumberOfMessagesVisible": 0}, later)
    world.cloud.lab_mark_processed(["order-1", "order-2"], at=later.isoformat())


def _lambda_metrics(cloud: FakeCloud, resource_id: str, now, unhealthy: bool) -> None:
    errors = 25 if unhealthy else 0
    cloud.set_metric(resource_id, "Errors", errors, now)
    cloud.set_metric(resource_id, "Invocations", 100, now)
    cloud.set_metric(resource_id, "Throttles", 0, now)
    cloud.set_metric(resource_id, "Duration", 200, now)


def _state(consumer_id: str, queue_id: str, retired_id: str) -> dict:
    return {
        "resources": [
            {
                "module": "module.payments",
                "type": "aws_lambda_function",
                "name": "payment",
                "instances": [
                    {
                        "attributes": {
                            "arn": consumer_id,
                            "function_name": "payment-worker",
                            "timeout": 30,
                            "tags": {"owner": "payments", "environment": "production"},
                        }
                    }
                ],
            },
            {
                "type": "aws_sqs_queue",
                "name": "payments",
                "instances": [
                    {
                        "attributes": {
                            "arn": queue_id,
                            "name": "payments",
                            "tags": {"owner": "payments"},
                        }
                    }
                ],
            },
            {
                "type": "aws_lambda_function",
                "name": "retired",
                "instances": [
                    {
                        "attributes": {
                            "arn": retired_id,
                            "function_name": "retired-worker",
                            "timeout": 10,
                            "tags": {"owner": "payments"},
                        }
                    }
                ],
            },
        ]
    }
