def handler(event, context):
    import os
    import time

    # FAULT is set by an operator in the lab. The agent never changes it.
    if os.environ.get("FAULT") == "timeout":
        time.sleep(int(os.environ.get("FAULT_SLEEP_SECONDS", "20")))
    return {"ok": True, "records": len(event.get("Records") or [])}
