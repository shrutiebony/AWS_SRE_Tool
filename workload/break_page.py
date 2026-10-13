"""Local break button for the tiny workload. Not the SRE demo.

Start from this folder: python break_page.py
Open http://127.0.0.1:8766/ and press Break.
Terraform for these resources is main.tf in this folder.
Repair stays on the SRE page.
"""

from __future__ import annotations

import json
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8766
REGION = "us-east-2"
DLQ_NAME = "tiny-workload-orders-dlq"
WORKER = "tiny-workload-worker"
FAILER = "tiny-workload-failer"
ROOT = Path(__file__).resolve().parent


def _aws(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["aws", "--region", REGION, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _queue_url(name: str) -> str:
    if name != DLQ_NAME:
        raise RuntimeError("This page only sends to the tiny workload dead-letter queue.")
    completed = _aws(["sqs", "get-queue-url", "--queue-name", name, "--output", "json"])
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Could not resolve the dead-letter queue.")
    url = str(json.loads(completed.stdout).get("QueueUrl") or "")
    if not url.endswith("/" + DLQ_NAME):
        raise RuntimeError("Refusing to send outside the tiny workload dead-letter queue.")
    return url


def _invoke(function_name: str, payload: dict) -> dict:
    if function_name not in {WORKER, FAILER}:
        raise RuntimeError("This page only invokes tiny workload functions.")
    outfile = ROOT / "build" / f"{function_name}-invoke.json"
    outfile.parent.mkdir(exist_ok=True)
    completed = _aws(
        [
            "lambda",
            "invoke",
            "--function-name",
            function_name,
            "--cli-binary-format",
            "raw-in-base64-out",
            "--payload",
            json.dumps(payload),
            str(outfile),
        ]
    )
    meta = {}
    if completed.stdout.strip():
        try:
            meta = json.loads(completed.stdout)
        except json.JSONDecodeError:
            meta = {"raw": completed.stdout.strip()[:300]}
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"{function_name} invoke failed.")
    meta["function"] = function_name
    return meta


def break_workload() -> dict:
    order_id = f"tiny-order-{int(time.time())}"
    logged = _invoke(WORKER, {"log_order": order_id})
    failed = _invoke(FAILER, {"order_id": order_id})
    body = json.dumps({"order_id": order_id})
    attributes = json.dumps(
        {"order_id": {"DataType": "String", "StringValue": order_id}}
    )
    sent = _aws(
        [
            "sqs",
            "send-message",
            "--queue-url",
            _queue_url(DLQ_NAME),
            "--message-body",
            body,
            "--message-attributes",
            attributes,
            "--output",
            "json",
        ]
    )
    if sent.returncode != 0:
        raise RuntimeError(sent.stderr.strip() or "Could not send the dead-letter message.")
    message = json.loads(sent.stdout)
    return {
        "order_id": order_id,
        "dlq": DLQ_NAME,
        "message_id": message.get("MessageId", ""),
        "worker_status": logged.get("StatusCode"),
        "failer_error": failed.get("FunctionError") or "",
        "failer_status": failed.get("StatusCode"),
    }


def _page(result: str = "", error: str = "") -> bytes:
    notice = ""
    if result:
        notice = f"<p>{result}</p>"
    if error:
        notice = f"<p>{error}</p>"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tiny workload</title>
</head>
<body>
  <h1>Tiny workload</h1>
  <p>This page is separate from the SRE health checker. Break sends one transient order onto tiny-workload-orders-dlq and invokes tiny-workload-failer so it errors.</p>
  <form method="post" action="/break">
    <button type="submit">Break</button>
  </form>
  {notice}
</body>
</html>
"""
    return html.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        self._send(200, _page(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/break":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            result = break_workload()
        except Exception as exc:
            self._send(500, _page(error=str(exc)[:500]), "text/html; charset=utf-8")
            return
        text = (
            f"Broke {result['dlq']} with order {result['order_id']} "
            f"(message {result['message_id']}). "
            f"Worker status {result['worker_status']}. "
            f"Failer status {result['failer_status']} error {result['failer_error'] or 'none'}."
        )
        self._send(200, _page(result=text), "text/html; charset=utf-8")

    def _send(self, code: int, payload: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Tiny workload break page at http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
