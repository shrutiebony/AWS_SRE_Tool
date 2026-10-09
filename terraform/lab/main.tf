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
  default = "enterprise-sites"
}

data "aws_caller_identity" "current" {}

data "archive_file" "directory" {
  type        = "zip"
  source_file = "${path.module}/directory.py"
  output_path = "${path.module}/build/directory.zip"
}

resource "aws_dynamodb_table" "sites" {
  name         = "${var.name_prefix}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "site_id"

  attribute {
    name = "site_id"
    type = "S"
  }
}

resource "aws_dynamodb_table_item" "sites" {
  for_each   = { for site in local.sites : site.site_id => site }
  table_name = aws_dynamodb_table.sites.name
  hash_key   = aws_dynamodb_table.sites.hash_key

  item = jsonencode({
    site_id       = { S = each.value.site_id }
    site_name     = { S = each.value.site_name }
    region        = { S = each.value.region }
    business_unit = { S = each.value.business_unit }
    cost_center   = { S = each.value.cost_center }
    status        = { S = each.value.status }
    owner         = { S = each.value.owner }
    headcount     = { N = tostring(each.value.headcount) }
    opened_on     = { S = each.value.opened_on }
  })
}

resource "aws_iam_role" "directory" {
  name = "${var.name_prefix}-directory"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "directory" {
  name = "site-table"
  role = aws_iam_role.directory.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan"]
        Resource = aws_dynamodb_table.sites.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
      }
    ]
  })
}

resource "aws_lambda_function" "directory" {
  function_name    = "${var.name_prefix}-directory"
  role             = aws_iam_role.directory.arn
  runtime          = "python3.12"
  handler          = "directory.handler"
  filename         = data.archive_file.directory.output_path
  source_code_hash = data.archive_file.directory.output_base64sha256
  timeout          = 10
  memory_size      = 128

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.sites.name
    }
  }
}

resource "aws_api_gateway_rest_api" "directory" {
  name        = "${var.name_prefix}"
  description = "Site directory used by the facilities program. This API is one service in a system the health checker does not otherwise see."
}

resource "aws_api_gateway_resource" "sites" {
  rest_api_id = aws_api_gateway_rest_api.directory.id
  parent_id   = aws_api_gateway_rest_api.directory.root_resource_id
  path_part   = "sites"
}

resource "aws_api_gateway_resource" "site" {
  rest_api_id = aws_api_gateway_rest_api.directory.id
  parent_id   = aws_api_gateway_resource.sites.id
  path_part   = "{siteId}"
}

resource "aws_api_gateway_method" "list" {
  rest_api_id   = aws_api_gateway_rest_api.directory.id
  resource_id   = aws_api_gateway_resource.sites.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_method" "get" {
  rest_api_id   = aws_api_gateway_rest_api.directory.id
  resource_id   = aws_api_gateway_resource.site.id
  http_method   = "GET"
  authorization = "NONE"

  request_parameters = {
    "method.request.path.siteId" = true
  }
}

resource "aws_api_gateway_method" "put" {
  rest_api_id   = aws_api_gateway_rest_api.directory.id
  resource_id   = aws_api_gateway_resource.site.id
  http_method   = "PUT"
  authorization = "NONE"

  request_parameters = {
    "method.request.path.siteId" = true
  }
}

resource "aws_api_gateway_integration" "list" {
  rest_api_id             = aws_api_gateway_rest_api.directory.id
  resource_id             = aws_api_gateway_resource.sites.id
  http_method             = aws_api_gateway_method.list.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.directory.invoke_arn
}

resource "aws_api_gateway_integration" "get" {
  rest_api_id             = aws_api_gateway_rest_api.directory.id
  resource_id             = aws_api_gateway_resource.site.id
  http_method             = aws_api_gateway_method.get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.directory.invoke_arn
}

resource "aws_api_gateway_integration" "put" {
  rest_api_id             = aws_api_gateway_rest_api.directory.id
  resource_id             = aws_api_gateway_resource.site.id
  http_method             = aws_api_gateway_method.put.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.directory.invoke_arn
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowSiteDirectoryApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.directory.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.directory.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "directory" {
  rest_api_id = aws_api_gateway_rest_api.directory.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_integration.list.id,
      aws_api_gateway_integration.get.id,
      aws_api_gateway_integration.put.id,
    ]))
  }

  depends_on = [
    aws_api_gateway_integration.list,
    aws_api_gateway_integration.get,
    aws_api_gateway_integration.put,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.directory.id
  deployment_id = aws_api_gateway_deployment.directory.id
  stage_name    = "prod"
}

locals {
  sites = [
    {
      site_id       = "SITE-1001"
      site_name     = "Seattle Campus"
      region        = "us-west-2"
      business_unit = "Cloud Operations"
      cost_center   = "CC-4401"
      status        = "active"
      owner         = "facilities.seattle@example.com"
      headcount     = 840
      opened_on     = "2014-03-01"
    },
    {
      site_id       = "SITE-1008"
      site_name     = "Austin Plant"
      region        = "us-east-1"
      business_unit = "Manufacturing"
      cost_center   = "CC-2210"
      status        = "active"
      owner         = "plant.austin@example.com"
      headcount     = 1260
      opened_on     = "2008-11-15"
    },
    {
      site_id       = "SITE-2044"
      site_name     = "Dublin Office"
      region        = "eu-west-1"
      business_unit = "Customer Support"
      cost_center   = "CC-1188"
      status        = "maintenance"
      owner         = "sites.dublin@example.com"
      headcount     = 310
      opened_on     = "2019-06-20"
    },
    {
      site_id       = "SITE-3102"
      site_name     = "Singapore Hub"
      region        = "ap-southeast-1"
      business_unit = "Logistics"
      cost_center   = "CC-5502"
      status        = "active"
      owner         = "logistics.singapore@example.com"
      headcount     = 190
      opened_on     = "2021-01-11"
    },
    {
      site_id       = "SITE-4410"
      site_name     = "Frankfurt Data Center"
      region        = "eu-central-1"
      business_unit = "Infrastructure"
      cost_center   = "CC-3307"
      status        = "active"
      owner         = "dc.frankfurt@example.com"
      headcount     = 42
      opened_on     = "2016-09-30"
    },
  ]
}

output "api_base_url" {
  value = "${aws_api_gateway_stage.prod.invoke_url}/sites"
}

output "table_name" {
  value = aws_dynamodb_table.sites.name
}

output "table_arn" {
  value = aws_dynamodb_table.sites.arn
}

output "function_name" {
  value = aws_lambda_function.directory.function_name
}

output "api_name" {
  value = aws_api_gateway_rest_api.directory.name
}
