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

resource "terraform_data" "account_guard" {
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == "414279120882"
      error_message = "Refusing to apply outside account 414279120882."
    }
  }
}

data "archive_file" "handler" {
  type        = "zip"
  source_file = "${path.module}/handler.py"
  output_path = "${path.module}/build/handler.zip"
}

resource "aws_sqs_queue" "dlq" {
  name = "tiny-workload-orders-dlq"
  tags = {
    Project = "tiny-workload"
  }
}

resource "aws_sqs_queue" "orders" {
  name                       = "tiny-workload-orders"
  visibility_timeout_seconds = 30
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
  tags = {
    Project = "tiny-workload"
  }
}

resource "aws_dynamodb_table" "orders" {
  name         = "tiny-workload-orders"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "order_id"

  attribute {
    name = "order_id"
    type = "S"
  }

  tags = {
    Project = "tiny-workload"
  }
}

resource "aws_iam_role" "lambda" {
  name = "tiny-workload-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "sqs" {
  name = "tiny-workload-read-orders"
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
      Resource = aws_sqs_queue.orders.arn
    }]
  })
}

resource "aws_lambda_function" "worker" {
  function_name    = "tiny-workload-worker"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.worker"
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  timeout          = 10
  memory_size      = 128

  depends_on = [aws_iam_role_policy_attachment.logs]

  tags = {
    Project = "tiny-workload"
  }
}

resource "aws_lambda_function" "failer" {
  function_name    = "tiny-workload-failer"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.failer"
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  timeout          = 10
  memory_size      = 128

  depends_on = [aws_iam_role_policy_attachment.logs]

  tags = {
    Project = "tiny-workload"
  }
}

resource "aws_lambda_event_source_mapping" "orders" {
  event_source_arn = aws_sqs_queue.orders.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1
  enabled          = true

  depends_on = [aws_iam_role_policy.sqs]
}

output "orders_queue_arn" {
  value = aws_sqs_queue.orders.arn
}

output "orders_queue_url" {
  value = aws_sqs_queue.orders.url
}

output "dlq_arn" {
  value = aws_sqs_queue.dlq.arn
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "worker_name" {
  value = aws_lambda_function.worker.function_name
}

output "failer_name" {
  value = aws_lambda_function.failer.function_name
}

output "table_name" {
  value = aws_dynamodb_table.orders.name
}
