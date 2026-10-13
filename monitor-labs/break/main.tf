# Apply ../working first, then this root.
# Expected checker signal: lambda.errors_after_deploy while the configuration
# change is still recent (LastModified within 30 minutes), otherwise lambda.errors.
# This root does not create a second function, API, or table. It reads working
# state, points TABLE_NAME at missing-table, and invokes the API until CloudWatch
# Errors is non-zero. The API and table stay the ones working created.
# The provisioner then runs `python -m sre_agent inventory`, which is what writes
# the Jira ticket. A page view does not.
# Applying working/ again restores the function env because working owns it.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-2"
}

data "terraform_remote_state" "working" {
  backend = "local"
  config = {
    path = "../working/terraform.tfstate"
  }
}

resource "terraform_data" "break_directory" {
  input = {
    function_name = data.terraform_remote_state.working.outputs.function_name
    api_base_url  = data.terraform_remote_state.working.outputs.api_base_url
  }

  provisioner "local-exec" {
    interpreter = ["powershell", "-NoProfile", "-File", "${path.module}/break.ps1"]
    command     = "break"
    environment = {
      LAB_FUNCTION_NAME = data.terraform_remote_state.working.outputs.function_name
      LAB_API_URL       = data.terraform_remote_state.working.outputs.api_base_url
    }
  }
}
