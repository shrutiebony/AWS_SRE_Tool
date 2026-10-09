data "archive_file" "agent" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/build/agent.zip"
  excludes    = ["**/__pycache__", "**/*.pyc"]
}

resource "aws_iam_role_policy_attachment" "worker_logs" {
  role       = aws_iam_role.discovery.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "worker" {
  function_name    = "${var.name_prefix}-worker"
  role             = aws_iam_role.discovery.arn
  runtime          = "python3.12"
  handler          = "sre_agent.worker.handler"
  filename         = data.archive_file.agent.output_path
  source_code_hash = data.archive_file.agent.output_base64sha256
  timeout          = 60
  memory_size      = 256

  depends_on = [
    data.archive_file.agent,
    aws_iam_role_policy.discovery,
    aws_iam_role_policy_attachment.worker_logs,
    aws_iam_role_policy.discovery_enqueue,
  ]

  environment {
    variables = {
      SRE_REGISTRY_TABLE     = aws_dynamodb_table.registry.name
      SRE_MODE               = var.agent_mode
      SRE_ACCOUNT_ID         = data.aws_caller_identity.current.account_id
      SRE_REGION             = var.aws_region
      SRE_APPROVAL_MODE      = var.approval_mode
      SRE_REQUIRE_APPROVAL   = var.approval_mode == "required" ? "true" : "false"
      SRE_APPROVERS          = join(",", var.approver_account_ids)
      LAB_TARGETS            = join(",", var.lab_targets)
      CONFLUENCE_BASE_URL    = var.confluence_base_url
      CONFLUENCE_PAGE_ID     = var.confluence_page_id
      CONFLUENCE_EMAIL       = var.confluence_email
      CONFLUENCE_SECRET_ARN  = var.confluence_secret_arn
      JIRA_BASE_URL          = var.jira_base_url
      JIRA_PROJECT           = var.jira_project
      JIRA_EMAIL             = var.jira_email
      JIRA_SECRET_ARN        = var.jira_secret_arn
      JIRA_ISSUE_TYPE        = var.jira_issue_type
      JIRA_TRANSITION        = var.jira_transition
      SRE_SERVICE_LIST_URI   = var.service_list_uri
      SRE_EXECUTOR_QUEUE_URL = aws_sqs_queue.repair.url
      SRE_ORDER_TABLE        = var.order_table_name
    }
  }
}

resource "aws_lambda_event_source_mapping" "work" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1
}

# The executor role is not attached to this worker. Automatic repair stays off
# until a separate function assumes aws_iam_role.executor and a lab target is set.

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "sre-agent-worker-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "The discovery worker failed. This alarm does not authorize a repair."
  dimensions = {
    FunctionName = aws_lambda_function.worker.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "work_queue_depth" {
  alarm_name          = "sre-agent-work-queue-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 20
  alarm_description   = "Discovery work is backing up."
  dimensions = {
    QueueName = aws_sqs_queue.work.name
  }
}

resource "aws_cloudwatch_metric_alarm" "catalog_load_failed" {
  alarm_name          = "sre-agent-catalog-load-failed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "CatalogLoadFailed"
  namespace           = "SRE/Agent"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_description   = "The Confluence catalog did not load. Health stays unknown."
}

resource "aws_cloudwatch_metric_alarm" "jira_sync_failed" {
  alarm_name          = "sre-agent-jira-sync-failed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "JiraSyncFailed"
  namespace           = "SRE/Agent"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_description   = "Jira synchronization failed."
}
