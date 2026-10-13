"""Read one item from the directory table. A missing table fails the invocation."""

import json
import os

import boto3


def handler(event, context):
    table_name = os.environ["TABLE_NAME"]
    boto3.resource("dynamodb").Table(table_name).get_item(Key={"id": "health"})
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"table": table_name}),
    }
