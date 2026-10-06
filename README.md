# SRE agent

The agent starts from resources that already exist.

```text
discover → registry → monitor → diagnose → policy gate → repair → verify
```

Terraform may have created some of those resources. This project does not assume that, and it does not import them just to watch them. Terraform state, when you have it, adds intended configuration and ownership. It is not a complete inventory. Terraform in `terraform/` deploys the agent itself in `us-east-2` by default: the registry table, the work queue, a packaged Lambda worker, the discovery, diagnosis, and executor roles, hourly reconciliation, a five-minute monitor, a one-minute verification schedule, and alarms for worker errors, executor errors, queue backlog, repair-queue age, repair-queue failures, catalog load, and Jira sync. `agent_mode` defaults to `recommend` and `repairs_enabled` defaults to false. `approval_mode` defaults to `required`. Set `approval_mode` to `lab` and pass `lab_targets` when the designated lab redrive should run without a per-incident approval. Queue mutations stay limited to the two lab queue ARNs. The executor role is not attached to the discovery worker. `terraform/lab/` is a separate workload used to practice one fault.

## Run the first version

```powershell
pip install -e .
pytest
python -m sre_agent demo
python -m sre_agent lab
```

`demo` inventories a local account and stops at a recommendation. `lab` validates the one repair, `sqs.redrive_dlq`, including the cases where it must not run. The lab never points at live AWS.

A live read stays in recommendation mode. The same environment variables the worker uses (`JIRA_*`, `CONFLUENCE_*`, `SRE_REGISTRY_TABLE`, `SRE_ORDER_TABLE`) also configure this command. Without an email and API token it still discovers, and it does not open Jira or read Confluence.

The agent reads the Atlassian connection from a Secrets Manager JSON secret or from environment variables. The JSON may include `token` (or `api_token` or `password`), `base_url` or `site`, `email`, `confluence_page_id`, `jira_project`, `jira_issue_type`, and `jira_transition`. `CONFLUENCE_*` and `JIRA_*` environment variables override the secret. If only `CONFLUENCE_SECRET_ARN` or `JIRA_SECRET_ARN` is set, both Confluence and Jira use that secret. Terraform variables `confluence_secret_arn` and `jira_secret_arn` default to empty, so the Lambda receives an ARN only when the operator sets it. Missing base URL, page or project, email, or token leaves that client unset and discovery still runs. Symptom tables on the Confluence page are the catalog. When the secret and the environment omit an issue type, the agent uses `Task`.

```powershell
pip install boto3
python -m sre_agent inventory --account 123456789012 --region us-east-2
```

## What the first version does

1. Scan one account and one Region for Lambda, SQS, EC2, DynamoDB, and API Gateway. Pagination follows continuation tokens. A later page that fails is a partial result, not complete coverage. The caller identity has to match the configured account. A denied call is recorded and is not treated as an empty result. Global services are called out as not scanned.
2. Upsert a registry record by account, Region, type, and stable id. Optional sources are Resource Explorer, AWS Config, Terraform state, and an SNS registration message. SNS is not required.
3. A successful list that no longer sees a resource marks it uncertain. Only a direct lookup that returns not found marks it deleted.
4. Attach a monitoring pack for Lambda, SQS, EC2, DynamoDB, and API Gateway. Anything else stays visible as discovered, monitoring unsupported. Missing required telemetry is unknown, not healthy. A configured Confluence catalog is validated, versioned, and evaluated, including metric math and Logs Insights queries. If that load fails, health stays unknown.
5. Diagnose with six read-only tools. Rules decide the action. An explanation hook cannot change it.
6. The policy gate defaults to recommendation. The only validated automatic repair is replaying verified transient orders from an SQS dead-letter queue, and only after you enable it. Classification comes from correlated application logs. A sender-supplied failure label is ignored. Malformed and permanently failing orders stay on the queue, and unsampled messages are not bulk-moved. A failing consumer, poison messages, and a Terraform-owned configuration change are refused. The executor rechecks policy and approval immediately before the mutation.
7. Verification waits for real elapsed time on a live clock and steps a simulated clock only in tests. A scheduled verify job runs the check, and the check times out if recovery is not confirmed. Recovery requires healthy telemetry and a consistent read showing each replayed `order_id` was processed after the repair. A record that already existed does not count. Cancelling a running move does not put already-moved messages back. A failed recovery records the lesson and blocks a repeat.
8. An open incident is reused for the same account, Region, resource, and problem type. Jira creates or updates that one issue and retries a failed sync. The issue type and the completion transition are configurable. The issue is resolved only after verified recovery. An approval is the Atlassian account ID of the comment author, checked against an explicit allow list, unless lab approval mode is set for the designated redrive. Text inside the comment cannot name the approver.
9. The discovery worker and the executor are separate Lambdas. Repairs stay off until `repairs_enabled` is true. Locks are conditional DynamoDB writes, including the update that releases a lock. The Confluence catalog must contain one table whose header has the rule columns; an introductory table is ignored. Catalog statistics and periods are applied to the returned points, and a Logs Insights stats row is read as an aggregate. Lambda health uses the latest period: older errors do not keep a restored consumer failed, and a missing or stale sample is not treated as healthy.

The application map stores the evidence for each relationship, so a queue backlog whose consumer is failing is not treated as a redrive.
