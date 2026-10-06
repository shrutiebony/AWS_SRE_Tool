from datetime import timedelta

from sre_agent.aws.errors import CloudError, classify_error_code
from sre_agent.aws.fake import FakeCloud, FunctionRecord
from sre_agent.aws.names import resources_from_terraform
from sre_agent.clock import Clock, parse_time
from sre_agent.discovery.service import ABSENCE, AccountTarget, Scope
from sre_agent.lab.scenarios import ACCOUNT, OTHER_ACCOUNT, REGION, build_world, lambda_arn
from sre_agent.models import ResourceRecord
from sre_agent.registry.store import MemoryTable, Registry
from sre_agent.service import OperationsAgent


def test_error_codes_map_denial_separately_from_absence():
    assert classify_error_code("AccessDenied") == "denied"
    assert classify_error_code("Throttling") == "throttled"
    assert classify_error_code("NonExistentQueue") == "not_found"


def test_pagination_and_throttle_do_not_drop_resources():
    clock = Clock()
    cloud = FakeCloud(ACCOUNT, REGION)
    for index in range(25):
        cloud.add_function(FunctionRecord(name=f"fn-{index}", arn=lambda_arn(f"fn-{index}")))
    cloud.throttle_remaining["list_functions"] = 2
    agent = _agent(cloud, clock)
    agent.inventory()
    found = agent.registry.list_resources(resource_type="lambda:function")
    assert len(found) == 25
    assert len({item.resource_id for item in found}) == 25


def test_partial_scan_does_not_declare_the_rest_missing():
    clock = Clock()
    cloud = FakeCloud(ACCOUNT, REGION)
    cloud.add_function(FunctionRecord(name="good", arn=lambda_arn("good")))
    cloud.add_function(FunctionRecord(name="bad", arn=lambda_arn("bad")))
    cloud.detail_errors["bad"] = CloudError("boom")
    agent = _agent(cloud, clock)
    agent.registry.upsert(_lambda("kept"), "authoritative")
    agent.inventory()
    assert agent.registry.get_by_resource_id(lambda_arn("good")).presence == "present"
    assert agent.registry.get_by_resource_id(lambda_arn("kept")).presence == "present"
    assert agent.registry.get_by_resource_id(lambda_arn("bad")) is None
    assert any(item.status == "partial" and item.collector == "lambda-api" for item in agent.registry.scans())


def test_denied_scan_does_not_mean_the_resource_is_gone():
    clock = Clock()
    cloud = FakeCloud(ACCOUNT, REGION)
    cloud.add_function(FunctionRecord(name="good", arn=lambda_arn("good")))
    agent = _agent(cloud, clock)
    agent.inventory()
    cloud.deny.add("list_functions")
    agent.inventory()
    assert agent.registry.get_by_resource_id(lambda_arn("good")).presence == "present"
    denied = [item for item in agent.registry.scans() if item.collector == "lambda-api" and item.status == "denied"]
    assert denied
    assert ABSENCE in denied[-1].message


def test_a_missing_resource_stays_uncertain_until_lookup_confirms_deletion():
    clock = Clock()
    cloud = FakeCloud(ACCOUNT, REGION)
    cloud.add_function(FunctionRecord(name="good", arn=lambda_arn("good")))
    agent = _agent(cloud, clock)
    agent.inventory()
    del cloud.functions["good"]
    agent.inventory()
    record = agent.registry.get_by_resource_id(lambda_arn("good"))
    assert record.presence == "uncertain"
    assert record.presence != "deleted"
    assert agent.discovery.refresh_one(lambda_arn("good")) == "deleted"
    assert agent.registry.get_by_resource_id(lambda_arn("good")).presence == "deleted"


def test_repeat_scans_do_not_duplicate_the_registry():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    agent.inventory()
    records = agent.registry.list_resources()
    assert len(records) == len({item.identity for item in records})
    assert len([item for item in records if item.resource_type == "lambda:function" and item.presence == "present"]) == 2


def test_terraform_state_adds_ownership_and_is_not_the_inventory():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    live = agent.registry.get_by_resource_id(world.consumer_id)
    phantom = agent.registry.get_by_resource_id(world.retired_id)
    instance = agent.registry.get_by_resource_id(world.instance_id)
    assert live.presence == "present"
    assert live.managed_by == "terraform"
    assert live.ownership["owner"] == "payments"
    assert phantom.presence == "uncertain"
    assert phantom.discovery_source == "terraform-state"
    assert instance.presence == "present"
    assert instance.managed_by == ""


def test_sns_registration_is_optional_metadata():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.discovery.ingest_sns({"resource_id": world.queue_id, "owner": "payments", "notes": "checkout"})
    agent.inventory()
    record = agent.registry.get_by_resource_id(world.queue_id)
    assert record.presence == "present"
    assert record.discovery_source == "sqs-api"
    assert record.metadata["owner"] == "payments"
    assert "sns" in record.sources
    bad = agent.discovery.ingest_sns({"notes": "missing id"})
    assert bad.status == "error"


def test_unsupported_explorer_resource_stays_visible():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    agent.attach_packs()
    bucket = agent.registry.get_by_resource_id(world.bucket_id)
    assert bucket.presence == "present"
    assert bucket.monitoring_status == "unsupported"
    assert bucket.public_view()["monitoring_status"] == "unsupported"


def test_unconfigured_sources_do_not_imply_an_empty_account():
    world = build_world(explorer=False, terraform=False)
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    results = agent.inventory()
    messages = {item.collector: item for item in results}
    for name in ("aws-config", "resource-explorer", "global-services", "terraform-state"):
        assert messages[name].status == "not-configured"
        assert ABSENCE in messages[name].message


def test_no_credentials_does_not_erase_another_account():
    world = build_world()
    other = ResourceRecord(
        resource_id=lambda_arn("other", account=OTHER_ACCOUNT),
        account_id=OTHER_ACCOUNT,
        region=REGION,
        resource_type="lambda:function",
        name="other",
        discovery_source="lambda-api",
        last_seen_at=world.clock.iso(),
        presence="present",
    )
    world.scope.accounts.append(AccountTarget(account_id=OTHER_ACCOUNT, regions=[REGION], reader=None))
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.registry.upsert(other, "authoritative")
    agent.inventory()
    assert agent.registry.get_by_resource_id(other.resource_id).presence == "present"
    denied = [item for item in agent.registry.scans() if item.collector == "account-access"]
    assert denied and ABSENCE in denied[-1].message


def test_relationships_keep_their_evidence():
    world = build_world()
    agent = OperationsAgent(world.scope, Registry(), world.clock)
    agent.inventory()
    relationships = agent.registry.relationships()
    consumes = [item for item in relationships if item.kind == "consumes"]
    dead = [item for item in relationships if item.kind == "dead_letters_to"]
    assert consumes[0].evidence["mapping_uuid"] == "esm-1"
    assert dead[0].target_id == world.dlq_id
    assert dead[0].evidence["redrive_policy"]["deadLetterTargetArn"] == world.dlq_id


def test_registry_document_roundtrip_uses_one_identity():
    table = MemoryTable()
    registry = Registry(table)
    record = _lambda("good")
    registry.upsert(record, "authoritative")
    registry.upsert(record, "authoritative")
    stored = [item for item in table.items.values() if item["collection"] == "resources"]
    assert len(stored) == 1
    assert stored[0]["identity"] == f"resources#{record.identity}"


def test_terraform_show_json_shape():
    document = {
        "values": {
            "root_module": {
                "resources": [
                    {
                        "type": "aws_instance",
                        "name": "web",
                        "values": {"id": "i-1", "availability_zone": "us-east-1a", "instance_type": "t3.micro", "tags": {}},
                    }
                ]
            }
        }
    }
    records, errors = resources_from_terraform(document, "2026-10-06T13:00:00+00:00", ACCOUNT, REGION)
    assert not errors
    assert records[0].resource_id.endswith("instance/i-1")


def test_direct_lookup_denial_does_not_delete():
    clock = Clock()
    cloud = FakeCloud(ACCOUNT, REGION)
    cloud.add_function(FunctionRecord(name="good", arn=lambda_arn("good")))
    agent = _agent(cloud, clock)
    agent.inventory()
    cloud.deny.add("lookup")
    assert agent.discovery.refresh_one(lambda_arn("good")) == "denied"
    assert agent.registry.get_by_resource_id(lambda_arn("good")).presence == "present"


def _agent(cloud, clock):
    scope = Scope([AccountTarget(ACCOUNT, [REGION], reader=cloud)], page_size=10)
    return OperationsAgent(scope, Registry(), clock)


def _lambda(name: str) -> ResourceRecord:
    return ResourceRecord(
        resource_id=lambda_arn(name),
        account_id=ACCOUNT,
        region=REGION,
        resource_type="lambda:function",
        name=name,
        discovery_source="lambda-api",
        last_seen_at="2026-10-06T13:00:00+00:00",
        presence="present",
    )


def test_parse_time_accepts_aws_offsets():
    stamp = parse_time("2026-10-01T00:00:00.000+0000")
    assert stamp.tzinfo is not None
    assert stamp + timedelta(0)
