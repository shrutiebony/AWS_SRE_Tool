variable "aws_region" {
  type        = string
  description = "Region for the agent infrastructure and the first discovery scope."
  default     = "us-east-2"
}

variable "name_prefix" {
  type        = string
  description = "Prefix for the agent registry, queue, and roles."
  default     = "sre-agent"
}

variable "enable_cloudtrail" {
  type        = bool
  description = "Create a trail so EventBridge can receive management events. Leave false until you intend to create one."
  default     = false
}

variable "cross_account_role_arns" {
  type        = list(string)
  description = "Roles the discovery worker may assume when another account is added to the scope."
  default     = []
}

variable "lab_queue_arns" {
  type        = list(string)
  description = "The source queue and its dead-letter queue. Mutations are limited to these two ARNs. Empty uses a placeholder that matches nothing."
  default     = []

  validation {
    condition     = length(var.lab_queue_arns) == 0 || length(var.lab_queue_arns) == 2
    error_message = "Pass the source queue and its dead-letter queue, or leave the list empty."
  }
}

variable "lab_kms_key_arns" {
  type        = list(string)
  description = "KMS keys for encrypted lab queues. Leave empty when the queues are not encrypted."
  default     = []
}

variable "confluence_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for the Confluence API token."
  default     = ""
}

variable "jira_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for the Jira API token."
  default     = ""
}

variable "confluence_base_url" {
  type        = string
  description = "Atlassian site origin. The client appends /wiki/api/v2/pages/{id}."
  default     = "https://sjsu-team-lpan6pj6.atlassian.net"
}

variable "confluence_page_id" {
  type        = string
  description = "AWS failure scenario catalog page."
  default     = "360481"
}

variable "confluence_email" {
  type        = string
  description = "Atlassian account email that can read the catalog page."
  default     = "shruti.goyal@sjsu.edu"
}

variable "jira_base_url" {
  type        = string
  description = "Atlassian site origin. The client calls /rest/api/3 on this host."
  default     = "https://sjsu-team-lpan6pj6.atlassian.net"
}

variable "jira_project" {
  type        = string
  description = "Jira project key for incidents."
  default     = "KAN"
}

variable "jira_email" {
  type        = string
  description = "Atlassian account email that can create issues in the Jira project."
  default     = "shruti.goyal@sjsu.edu"
}

variable "order_table_name" {
  type        = string
  description = "DynamoDB table of application orders. Recovery checks read status = processed for order_id."
  default     = ""
}

variable "order_table_arn" {
  type        = string
  description = "ARN of the order table. Leave empty until that table exists."
  default     = ""
}

variable "agent_mode" {
  type        = string
  description = "Discovery worker mode. recommend records incidents. automatic may enqueue the lab redrive."
  default     = "recommend"

  validation {
    condition     = contains(["recommend", "automatic"], var.agent_mode)
    error_message = "agent_mode must be recommend or automatic."
  }
}

variable "repairs_enabled" {
  type        = bool
  description = "When false, the executor Lambda returns without mutating a queue."
  default     = false
}

variable "lab_targets" {
  type        = list(string)
  description = "Resource ARNs passed to both Lambdas. Lab approval mode may repair only these targets."
  default     = []
}

variable "approval_mode" {
  type        = string
  description = "required waits for an approver comment. lab runs sqs.redrive_dlq on a lab target without a per-incident approval."
  default     = "required"

  validation {
    condition     = contains(["required", "lab"], var.approval_mode)
    error_message = "approval_mode must be required or lab."
  }
}

variable "approver_account_ids" {
  type        = list(string)
  description = "Atlassian account IDs allowed to approve a repair. Empty fails closed when approval is required."
  default     = []
}

variable "jira_issue_type" {
  type        = string
  description = "Jira issue type created for an incident. KAN provides Task, not Incident."
  default     = "Task"
}

variable "jira_transition" {
  type        = string
  description = "Workflow transition used only after verified recovery."
  default     = "Done"
}
