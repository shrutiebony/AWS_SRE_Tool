aws_region        = "us-east-2"
name_prefix       = "sre-agent"
agent_mode        = "recommend"
repairs_enabled   = false
approval_mode     = "required"
enable_cloudtrail = false

order_table_name = "agent-lab-orders"
order_table_arn  = "arn:aws:dynamodb:us-east-2:414279120882:table/agent-lab-orders"

lab_queue_arns = [
  "arn:aws:sqs:us-east-2:414279120882:agent-lab-orders",
  "arn:aws:sqs:us-east-2:414279120882:agent-lab-failed-orders"
]