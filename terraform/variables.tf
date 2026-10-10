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
  description = "Secrets Manager ARN for the Atlassian connection used to read Confluence. Default is empty, so the Lambda receives this ARN only when the operator sets the variable. A JSON secret may include token (or api_token or password), base_url or site, email, confluence_page_id, jira_project, jira_issue_type, and jira_transition. When jira_secret_arn is empty, Jira uses this secret too."
  default     = ""
}

variable "jira_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for the Atlassian connection used to update Jira. Default is empty, so the Lambda receives this ARN only when the operator sets the variable. When this is empty and confluence_secret_arn is set, Jira uses that secret."
  default     = ""
}

variable "confluence_base_url" {
  type        = string
  description = "Optional override for the Atlassian site origin. The client appends /wiki/api/v2/pages/{id}. Empty uses base_url or site from the Secrets Manager JSON."
  default     = ""
}

variable "confluence_page_id" {
  type        = string
  description = "Optional override for the Confluence catalog page id. Empty uses confluence_page_id from the Secrets Manager JSON."
  default     = ""
}

variable "confluence_email" {
  type        = string
  description = "Optional override for the Atlassian account email. Empty uses email from the Secrets Manager JSON."
  default     = ""
}

variable "jira_base_url" {
  type        = string
  description = "Optional override for the Jira site origin. The client calls /rest/api/3 on this host. Empty uses base_url or site from the Secrets Manager JSON."
  default     = ""
}

variable "jira_project" {
  type        = string
  description = "Optional override for the Jira project key. Empty uses jira_project from the Secrets Manager JSON."
  default     = ""
}

variable "jira_email" {
  type        = string
  description = "Optional override for the Atlassian account email used with Jira. Empty uses email from the Secrets Manager JSON."
  default     = ""
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
  description = "Optional override for the Jira issue type. Empty uses jira_issue_type from the secret. When the secret and the environment both omit it, the agent uses Task."
  default     = ""
}

variable "jira_transition" {
  type        = string
  description = "Jira transition name used when a verified repair closes an issue."
  default     = "Done"
}

variable "service_list_uri" {
  type        = string
  description = "Optional s3://bucket/key JSON list of extra resource types to inventory. Empty monitors the built-in types plus every typed service on the Confluence page."
  default     = ""
}
