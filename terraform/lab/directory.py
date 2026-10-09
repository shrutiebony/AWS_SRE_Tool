"""Site directory for one slice of a larger facilities system.

Callers that this service cannot see look up a campus before they open work
orders. The function only reads and writes the site table.
"""

import json
import os
from decimal import Decimal

import boto3

_table = None


def handler(event, context):
    del context
    method = event.get("httpMethod", "")
    site_id = (event.get("pathParameters") or {}).get("siteId")
    if method == "GET" and not site_id:
        result = _sites().scan()
        return _response(200, {"sites": _convert(result.get("Items", []))})
    if method == "GET" and site_id:
        result = _sites().get_item(Key={"site_id": site_id})
        item = result.get("Item")
        if item is None:
            return _response(404, {"message": "Site not found", "site_id": site_id})
        return _response(200, _convert(item))
    if method == "PUT" and site_id:
        body = json.loads(event.get("body") or "{}")
        if not isinstance(body, dict):
            return _response(400, {"message": "Body must be an object"})
        item = {"site_id": site_id, **{key: value for key, value in body.items() if key != "site_id"}}
        _sites().put_item(Item=item)
        return _response(200, _convert(item))
    return _response(400, {"message": "Use GET /sites, GET /sites/{siteId}, or PUT /sites/{siteId}"})


def _sites():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _convert(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_convert(item) for item in value]
    if isinstance(value, dict):
        return {key: _convert(item) for key, item in value.items()}
    return value
