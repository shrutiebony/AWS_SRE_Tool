"""Small orders API. POST creates an order. GET reads it back from DynamoDB."""

import json
import os

import boto3

_table = None


def _orders():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def handler(event, context):
    del context
    method = event.get("httpMethod", "")
    order_id = ((event.get("pathParameters") or {}).get("order_id") or "").strip()
    if method == "POST" and not order_id:
        return _create(event)
    if method == "GET" and order_id:
        return _read(order_id)
    return _response(404, {"error": "Use POST /orders or GET /orders/{order_id}."})


def _create(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Body must be JSON."})
    if not isinstance(body, dict):
        return _response(400, {"error": "Body must be a JSON object."})
    order_id = str(body.get("order_id") or "").strip()
    if not order_id:
        return _response(400, {"error": "order_id is required."})
    item = {"order_id": order_id, "status": "received", "item": str(body.get("item") or "widget")}
    _orders().put_item(Item=item)
    return _response(201, item)


def _read(order_id):
    item = _orders().get_item(Key={"order_id": order_id}).get("Item")
    if item is None:
        return _response(404, {"error": "Order was not found."})
    return _response(200, item)


def _response(status, body):
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}
