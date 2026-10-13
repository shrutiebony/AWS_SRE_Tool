"""Fault-lab worker.

A message is a transient failure only until its own fail_before time. The
function still returns successfully and reports that one item, so the consumer
stays healthy while the message is sent to the dead-letter queue. A later
replay of the same body is accepted.
"""

import json
import time


def handler(event, context):
    records = event.get("Records") or []
    if not records:
        print(json.dumps({"event": "ok", "detail": "health probe succeeded"}))
        return {"ok": True}
    failures = []
    for record in records:
        body = record.get("body") or ""
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        order_id = str(payload.get("order_id") or "")
        try:
            fail_before = float(payload.get("fail_before") or 0)
        except (TypeError, ValueError):
            fail_before = 0
        if order_id and fail_before and time.time() < fail_before:
            print(
                json.dumps(
                    {
                        "event": "transient_failure",
                        "order_id": order_id,
                        "detail": "connection reset by peer",
                    }
                )
            )
            message_id = record.get("messageId")
            if message_id:
                failures.append({"itemIdentifier": message_id})
            continue
        print(json.dumps({"event": "processed", "order_id": order_id, "detail": "accepted"}))
    return {"batchItemFailures": failures}
