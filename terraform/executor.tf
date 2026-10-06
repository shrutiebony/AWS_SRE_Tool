resource "aws_sqs_queue" "repair_dlq" {
  name = "${var.name_prefix}-repair-dlq"
}

resource "aws_sqs_queue" "repair" {
  name                       = "${var.name_prefix}-repair"
  visibility_timeout_seconds = 360
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.repair_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_iam_role_policy" "discovery_enqueue" {
  name = "enqueue-repair"
  role = aws_iam_role.discovery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.repair.arn
    }]
  })
}

resource "aws_iam_role_policy" "executor_reads" {
  name = "executor-recheck"
  role = aws_iam_role.executor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid    = "RecheckBeforeMutation"
          Effect = "Allow"
          Action = [
            "cloudwatch:GetMetricData",
            "logs:FilterLogEvents",
            "logs:StartQuery",
            "logs:GetQueryResults",
            "lambda:GetFunction",
            "lambda:ListEventSourceMappings",
            "sqs:GetQueueAttributes",
            "sqs:GetQueueUrl",
          ]
          Resource = "*"
        },
      ],
      length(local.secret_arns) == 0 ? [] : [
        {
          Sid      = "ReadConfiguredSecrets"
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = local.secret_arns
        },
      ],
      var.order_table_arn == "" ? [] : [
        {
          Sid      = "ReadOrderProcessing"
          Effect   = "Allow"
          Action   = ["dynamodb:GetItem"]
          Resource = var.order_table_arn
        },
      ]
    )
  })
}

resource "aws_iam_role_policy" "discovery_orders" {
  count = var.order_table_arn == "" ? 0 : 1
  name  = "read-orders"
  role  = aws_iam_role.discovery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem"]
      Resource = var.order_table_arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "executor_logs" {
  role       = aws_iam_role.executor.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "executor_queue" {
  name = "consume-repair"
  role = aws_iam_role.executor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
      Resource = aws_sqs_queue.repair.arn
    }]
  })
}

resource "aws_lambda_function" "executor" {
  function_name    = "${var.name_prefix}-executor"
  role             = aws_iam_role.executor.arn
  runtime          = "python3.12"
  handler          = "sre_agent.executor_worker.handler"
  filename         = data.archive_file.agent.output_path
  source_code_hash = data.archive_file.agent.output_base64sha256
  timeout          = 60
  memory_size      = 256

  depends_on = [
    data.archive_file.agent,
    aws_iam_role_policy.executor,
    aws_iam_role_policy.executor_reads,
    aws_iam_role_policy.executor_queue,
    aws_iam_role_policy_attachment.executor_logs,
  ]

  environment {
    variables = {
      SRE_REGISTRY_TABLE    = aws_dynamodb_table.registry.name
      SRE_ACCOUNT_ID        = data.aws_caller_identity.current.account_id
      SRE_REGION            = var.aws_region
      SRE_REPAIRS_ENABLED   = var.repairs_enabled ? "true" : "false"
      SRE_APPROVAL_MODE     = var.approval_mode
      SRE_REQUIRE_APPROVAL  = var.approval_mode == "required" ? "true" : "false"
      SRE_APPROVERS         = join(",", var.approver_account_ids)
      LAB_TARGETS           = join(",", var.lab_targets)
      SRE_ORDER_TABLE       = var.order_table_name
      JIRA_BASE_URL         = var.jira_base_url
      JIRA_PROJECT          = var.jira_project
      JIRA_EMAIL            = var.jira_email
      JIRA_SECRET_ARN       = var.jira_secret_arn
      JIRA_ISSUE_TYPE       = var.jira_issue_type
      JIRA_TRANSITION       = var.jira_transition
      CONFLUENCE_BASE_URL   = var.confluence_base_url
      CONFLUENCE_PAGE_ID    = var.confluence_page_id
      CONFLUENCE_EMAIL      = var.confluence_email
      CONFLUENCE_SECRET_ARN = var.confluence_secret_arn
    }
  }
}

resource "aws_lambda_event_source_mapping" "repair" {
  event_source_arn = aws_sqs_queue.repair.arn
  function_name    = aws_lambda_function.executor.arn
  batch_size       = 1
}

resource "aws_cloudwatch_metric_alarm" "executor_errors" {
  alarm_name          = "sre-agent-executor-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "The executor Lambda failed. This alarm does not authorize another repair."
  dimensions = {
    FunctionName = aws_lambda_function.executor.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "repair_queue_age" {
  alarm_name          = "sre-agent-repair-queue-age"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 300
  alarm_description   = "A repair message has been waiting on the executor queue."
  dimensions = {
    QueueName = aws_sqs_queue.repair.name
  }
}

resource "aws_cloudwatch_metric_alarm" "repair_dlq_depth" {
  alarm_name          = "sre-agent-repair-dlq-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "The repair queue dead-lettered a message."
  dimensions = {
    QueueName = aws_sqs_queue.repair_dlq.name
  }
}
