terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4.0"
    }
  }
}

provider "aws" {
  region = "us-east-2"
}

data "aws_caller_identity" "current" {}

data "archive_file" "handler" {
  type        = "zip"
  source_file = "${path.module}/handler.py"
  output_path = "${path.module}/build/handler.zip"
}

resource "aws_iam_role" "lambda" {
  name = "sre-fault-lab-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_sqs" {
  name = "sre-fault-lab-sqs"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ChangeMessageVisibility",
      ]
      Resource = aws_sqs_queue.jobs.arn
    }]
  })
}

resource "aws_lambda_function" "worker" {
  function_name    = "sre-fault-lab-worker"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  timeout          = 10
  memory_size      = 128

  depends_on = [aws_iam_role_policy_attachment.lambda_logs]
}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn        = aws_sqs_queue.jobs.arn
  function_name           = aws_lambda_function.worker.arn
  batch_size              = 1
  enabled                 = true
  function_response_types = ["ReportBatchItemFailures"]

  depends_on = [aws_iam_role_policy.lambda_sqs]
}

resource "aws_sqs_queue" "dlq" {
  name                      = "sre-fault-lab-jobs-dlq"
  message_retention_seconds = 3600
}

resource "aws_sqs_queue" "jobs" {
  name                      = "sre-fault-lab-jobs"
  message_retention_seconds = 3600
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 1
  })
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "lambda_name" {
  value = aws_lambda_function.worker.function_name
}

output "queue_url" {
  value = aws_sqs_queue.jobs.url
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "queue_arn" {
  value = aws_sqs_queue.jobs.arn
}

output "dlq_arn" {
  value = aws_sqs_queue.dlq.arn
}
