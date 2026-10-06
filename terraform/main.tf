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

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

resource "aws_dynamodb_table" "registry" {
  name         = "${var.name_prefix}-registry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "identity"

  attribute {
    name = "identity"
    type = "S"
  }

  attribute {
    name = "collection"
    type = "S"
  }

  global_secondary_index {
    name            = "collection-index"
    hash_key        = "collection"
    projection_type = "ALL"
  }
}

resource "aws_sqs_queue" "work_dlq" {
  name = "${var.name_prefix}-work-dlq"
}

resource "aws_sqs_queue" "work" {
  name                       = "${var.name_prefix}-work"
  visibility_timeout_seconds = 360
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.work_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_policy" "work" {
  queue_url = aws_sqs_queue.work.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.work.arn
      Condition = {
        ArnEquals = {
          "aws:SourceArn" = [
            aws_cloudwatch_event_rule.reconcile.arn,
            aws_cloudwatch_event_rule.monitor.arn,
            aws_cloudwatch_event_rule.verify.arn,
            aws_cloudwatch_event_rule.cloudtrail_changes.arn,
            aws_cloudwatch_event_rule.alarm_state.arn,
          ]
        }
      }
    }]
  })
}

resource "aws_cloudwatch_event_rule" "reconcile" {
  name                = "${var.name_prefix}-reconcile"
  description         = "Hourly reconciliation. Events alone are not a complete inventory."
  schedule_expression = "rate(1 hour)"
}

resource "aws_cloudwatch_event_rule" "monitor" {
  name                = "${var.name_prefix}-monitor"
  description         = "Recurring monitoring and recovery checks between reconciliations."
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "monitor" {
  rule      = aws_cloudwatch_event_rule.monitor.name
  target_id = "work-queue"
  arn       = aws_sqs_queue.work.arn
  input     = jsonencode({ type = "monitor" })
}

resource "aws_cloudwatch_event_rule" "verify" {
  name                = "${var.name_prefix}-verify"
  description         = "Recovery checks run on this schedule instead of waiting for the next unrelated message."
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "verify" {
  rule      = aws_cloudwatch_event_rule.verify.name
  target_id = "work-queue"
  arn       = aws_sqs_queue.work.arn
  input     = jsonencode({ type = "verify" })
}

resource "aws_cloudwatch_event_target" "reconcile" {
  rule      = aws_cloudwatch_event_rule.reconcile.name
  target_id = "work-queue"
  arn       = aws_sqs_queue.work.arn
  input     = jsonencode({ type = "reconcile" })
}

resource "aws_cloudwatch_event_rule" "cloudtrail_changes" {
  name        = "${var.name_prefix}-cloudtrail-changes"
  description = "Refresh a resource after a relevant API call. Requires a CloudTrail trail before events arrive."
  event_pattern = jsonencode({
    source      = ["aws.lambda", "aws.sqs", "aws.ec2", "aws.dynamodb", "aws.apigateway"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventName = [
        "CreateFunction20150331",
        "DeleteFunction20150331",
        "UpdateFunctionCode20150331",
        "UpdateFunctionConfiguration20150331",
        "CreateQueue",
        "DeleteQueue",
        "SetQueueAttributes",
        "RunInstances",
        "TerminateInstances",
        "CreateTable",
        "DeleteTable",
        "CreateRestApi",
        "DeleteRestApi",
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "cloudtrail_changes" {
  rule      = aws_cloudwatch_event_rule.cloudtrail_changes.name
  target_id = "work-queue"
  arn       = aws_sqs_queue.work.arn
}

resource "aws_cloudwatch_event_rule" "alarm_state" {
  name        = "${var.name_prefix}-alarm-state"
  description = "An alarm starts an investigation. It does not authorize a repair."
  event_pattern = jsonencode({
    source      = ["aws.cloudwatch"]
    detail-type = ["CloudWatch Alarm State Change"]
  })
}

resource "aws_cloudwatch_event_target" "alarm_state" {
  rule      = aws_cloudwatch_event_rule.alarm_state.name
  target_id = "work-queue"
  arn       = aws_sqs_queue.work.arn
}

resource "aws_cloudwatch_metric_alarm" "work_queue_age" {
  alarm_name          = "sre-agent-work-queue-age"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 300
  alarm_description   = "Discovery work is waiting. This alarm does not authorize a repair."
  dimensions = {
    QueueName = aws_sqs_queue.work.name
  }
}

resource "aws_s3_bucket" "trail" {
  count         = var.enable_cloudtrail ? 1 : 0
  bucket_prefix = "${var.name_prefix}-trail-"
}

resource "aws_s3_bucket_public_access_block" "trail" {
  count                   = var.enable_cloudtrail ? 1 : 0
  bucket                  = aws_s3_bucket.trail[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "trail" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.trail[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.trail[0].arn
        Condition = {
          StringEquals = {
            "aws:SourceArn" = "arn:${data.aws_partition.current.partition}:cloudtrail:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:trail/${var.name_prefix}"
          }
        }
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.trail[0].arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl"  = "bucket-owner-full-control"
            "aws:SourceArn" = "arn:${data.aws_partition.current.partition}:cloudtrail:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:trail/${var.name_prefix}"
          }
        }
      }
    ]
  })
}

resource "aws_cloudtrail" "agent" {
  count                         = var.enable_cloudtrail ? 1 : 0
  name                          = var.name_prefix
  s3_bucket_name                = aws_s3_bucket.trail[0].id
  is_multi_region_trail         = true
  enable_log_file_validation    = true
  include_global_service_events = true
  depends_on                    = [aws_s3_bucket_policy.trail]
}
