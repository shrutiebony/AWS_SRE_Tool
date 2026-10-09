data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com", "ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "discovery" {
  name               = "${var.name_prefix}-discovery"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role" "diagnosis" {
  name               = "${var.name_prefix}-diagnosis"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role" "executor" {
  name               = "${var.name_prefix}-executor"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

locals {
  executor_queue_arns = length(var.lab_queue_arns) == 0 ? ["arn:${data.aws_partition.current.partition}:sqs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:lab-target-none"] : var.lab_queue_arns
  secret_arns         = compact([var.confluence_secret_arn, var.jira_secret_arn])
  service_list_parts  = var.service_list_uri == "" ? [] : split("/", trimprefix(var.service_list_uri, "s3://"))
  service_list_bucket = length(local.service_list_parts) == 0 ? "" : local.service_list_parts[0]
  service_list_key = length(local.service_list_parts) < 2 ? "" : join("/", slice(local.service_list_parts, 1, length(local.service_list_parts)))
  service_list_object_arn = local.service_list_bucket == "" ? "" : "arn:${data.aws_partition.current.partition}:s3:::${local.service_list_bucket}/${local.service_list_key}"
  discovery_statements = concat(
    [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeTags",
          "lambda:ListFunctions",
          "lambda:GetFunction",
          "lambda:ListEventSourceMappings",
          "lambda:ListTags",
          "sqs:ListQueues",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ListQueueTags",
          "cloudwatch:GetMetricData",
          "cloudwatch:DescribeAlarms",
          "logs:FilterLogEvents",
          "logs:StartQuery",
          "logs:GetQueryResults",
          "dynamodb:ListTables",
          "dynamodb:DescribeTable",
          "apigateway:GET",
          "sts:GetCallerIdentity",
          "cloudwatch:PutMetricData",
          "resource-explorer-2:Search",
          "config:ListDiscoveredResources",
          "config:SelectResourceConfig",
          "tag:GetResources",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = [
          aws_dynamodb_table.registry.arn,
          "${aws_dynamodb_table.registry.arn}/index/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:SendMessage"]
        Resource = [aws_sqs_queue.work.arn]
      },
    ],
    length(var.cross_account_role_arns) == 0 ? [] : [
      {
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = var.cross_account_role_arns
      },
    ],
    length(local.secret_arns) == 0 ? [] : [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = local.secret_arns
      },
    ]
  )
}

resource "aws_iam_role_policy" "discovery" {
  name = "discovery-read"
  role = aws_iam_role.discovery.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.discovery_statements
  })
}

resource "aws_iam_role_policy" "service_list" {
  count = local.service_list_object_arn == "" ? 0 : 1
  name  = "service-list"
  role  = aws_iam_role.discovery.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = local.service_list_object_arn
    }]
  })
}

resource "aws_iam_role_policy" "diagnosis" {
  name = "diagnosis-read"
  role = aws_iam_role.diagnosis.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadOnlyDiagnosis"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "lambda:GetFunction",
          "lambda:ListEventSourceMappings",
          "sqs:GetQueueAttributes",
          "sqs:ListQueueTags",
          "cloudwatch:GetMetricData",
          "cloudwatch:DescribeAlarms",
          "logs:FilterLogEvents",
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "executor" {
  name = "executor-redrive"
  role = aws_iam_role.executor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ValidatedRedriveOnly"
        Effect = "Allow"
        Action = [
          "sqs:StartMessageMoveTask",
          "sqs:CancelMessageMoveTask",
          "sqs:ListMessageMoveTasks",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:SendMessage",
          "sqs:ChangeMessageVisibility",
        ]
        Resource = local.executor_queue_arns
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.aws_region
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = length(var.lab_kms_key_arns) == 0 ? ["arn:${data.aws_partition.current.partition}:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:key/00000000-0000-0000-0000-000000000000"] : var.lab_kms_key_arns
        Condition = {
          StringEquals = {
            "kms:ViaService" = "sqs.${var.aws_region}.amazonaws.com"
          }
        }
      },
      {
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = [
          aws_dynamodb_table.registry.arn,
          "${aws_dynamodb_table.registry.arn}/index/*",
        ]
      },
    ]
  })
}
