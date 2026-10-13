

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

data "archive_file" "handler" {
  type        = "zip"
  source_file = "${path.module}/handler.py"
  output_path = "${path.module}/build/handler.zip"
}

resource "aws_dynamodb_table" "directory" {
  name         = "sre-dir-lab"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
}

resource "aws_iam_role" "directory" {
  name = "sre-dir-lab"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "directory_logs" {
  role       = aws_iam_role.directory.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "directory_table" {
  name = "sre-dir-lab-table"
  role = aws_iam_role.directory.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem"]
      Resource = aws_dynamodb_table.directory.arn
    }]
  })
}

resource "aws_lambda_function" "directory" {
  function_name    = "sre-dir-lab"
  role             = aws_iam_role.directory.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.handler.output_path
  source_code_hash = data.archive_file.handler.output_base64sha256
  timeout          = 10
  memory_size      = 128

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.directory.name
    }
  }

  depends_on = [aws_iam_role_policy_attachment.directory_logs]
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "sre-dir-lab-lambda-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  dimensions = {
    FunctionName = aws_lambda_function.directory.function_name
  }
}

resource "aws_api_gateway_rest_api" "directory" {
  name = "sre-dir-lab"
}

resource "aws_api_gateway_resource" "health" {
  rest_api_id = aws_api_gateway_rest_api.directory.id
  parent_id   = aws_api_gateway_rest_api.directory.root_resource_id
  path_part   = "health"
}

resource "aws_api_gateway_method" "health" {
  rest_api_id   = aws_api_gateway_rest_api.directory.id
  resource_id   = aws_api_gateway_resource.health.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "health" {
  rest_api_id             = aws_api_gateway_rest_api.directory.id
  resource_id             = aws_api_gateway_resource.health.id
  http_method             = aws_api_gateway_method.health.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.directory.invoke_arn
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowDirectoryApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.directory.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.directory.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "directory" {
  rest_api_id = aws_api_gateway_rest_api.directory.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_integration.health.id,
      aws_api_gateway_method.health.id,
    ]))
  }

  depends_on = [aws_api_gateway_integration.health]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.directory.id
  deployment_id = aws_api_gateway_deployment.directory.id
  stage_name    = "prod"
}

output "table_name" {
  value = aws_dynamodb_table.directory.name
}

output "function_name" {
  value = aws_lambda_function.directory.function_name
}

output "api_base_url" {
  value = "https://${aws_api_gateway_rest_api.directory.id}.execute-api.us-east-2.amazonaws.com/${aws_api_gateway_stage.prod.stage_name}/health"
}
