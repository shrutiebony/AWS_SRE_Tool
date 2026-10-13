"""Tiny workload handlers. The worker logs a transient order and processes queue messages. The failer raises."""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _transient_line(order_id: str) -> str:
    return json.dumps(
        {
            "event": "transient_failure",
            "order_id": order_id,
            "detail": "Task timed out after 30.00 seconds",
        }
    )


def worker(event, context):
    """Consumer. A direct log_order call records a transient failure and returns. Queue records are processed."""
    del context
    payload = event if isinstance(event, dict) else {}
    order_id = str(payload.get("log_order") or "")
    if order_id:
        logger.info(_transient_line(order_id))
        return {"logged": order_id}
    for record in payload.get("Records") or []:
        body = record.get("body") or ""
        try:
            message = json.loads(body)
        except (TypeError, ValueError):
            message = {}
        seen = str(message.get("order_id") or "") if isinstance(message, dict) else ""
        logger.info(json.dumps({"event": "processed", "order_id": seen}))
    return {"ok": True}


def failer(event, context):
    """Direct invoke raises so CloudWatch Errors increases. This function is not a queue consumer."""
    del context
    payload = event if isinstance(event, dict) else {}
    order_id = str(payload.get("order_id") or "tiny-workload")
    logger.info(_transient_line(order_id))
    raise RuntimeError("tiny-workload forced error")
