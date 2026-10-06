output "registry_table" {
  value = aws_dynamodb_table.registry.name
}

output "work_queue_url" {
  value = aws_sqs_queue.work.id
}

output "discovery_role_arn" {
  value = aws_iam_role.discovery.arn
}

output "diagnosis_role_arn" {
  value = aws_iam_role.diagnosis.arn
}

output "executor_role_arn" {
  value = aws_iam_role.executor.arn
}

output "worker_function" {
  value = aws_lambda_function.worker.function_name
}

output "executor_function" {
  value = aws_lambda_function.executor.function_name
}

output "repair_queue_url" {
  value = aws_sqs_queue.repair.id
}
