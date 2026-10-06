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
  region = var.aws_region
}

variable "aws_region" {
  type    = string
  default = "us-east-2"
}

variable "name_prefix" {
  type    = string
  default = "sre-lab"
}

data "archive_file" "consumer" {
  type        = "zip"
  source_file = "${path.module}/consumer.py"
  output_path = "${path.module}/build/consumer.zip"
}

resource "aws_sqs_queue" "dlq" {
  name = "${var.name_prefix}-payments-dlq"
}

resource "aws_sqs_queue" "payments" {
  name = "${var.name_prefix}-payments"
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_iam_role" "consumer" {
  name = "${var.name_prefix}-consumer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "consumer" {
  name = "consume"
  role = aws_iam_role.consumer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.payments.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_lambda_function" "consumer" {
  function_name    = "${var.name_prefix}-payment-worker"
  role             = aws_iam_role.consumer.arn
  runtime          = "python3.12"
  handler          = "consumer.handler"
  filename         = data.archive_file.consumer.output_path
  source_code_hash = data.archive_file.consumer.output_base64sha256
  timeout          = 10

  environment {
    variables = {
      FAULT = ""
    }
  }
}

resource "aws_lambda_event_source_mapping" "payments" {
  event_source_arn = aws_sqs_queue.payments.arn
  function_name    = aws_lambda_function.consumer.arn
  batch_size       = 1
}

output "payments_queue_arn" {
  value = aws_sqs_queue.payments.arn
}

output "dlq_arn" {
  value = aws_sqs_queue.dlq.arn
}

output "consumer_name" {
  value = aws_lambda_function.consumer.function_name
}
