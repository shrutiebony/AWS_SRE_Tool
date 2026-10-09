"""MCP stdio server for the same operations cycle.

Tools are read-only except sre_repair, which ignores any runbook the client
sends and executes only a decision the policy gate already marked execute.
The local session uses the lab fixture in recommendation mode.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

from sre_agent.lab.scenarios import build_world
from sre_agent.models import AgentConfig
from sre_agent.registry.store import Registry
from sre_agent.report import format_report
from sre_agent.service import OperationsAgent

PROTOCOL = "2024-11-05"

_TOOLS = [
    {
        "name": "sre_run_cycle",
        "description": "Discover, monitor, and diagnose the local lab. Recommendation mode does not repair.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    {
        "name": "sre_explain",
        "description": "Return the grounded explanation for the latest cycle. Ungrounded model text is dropped.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    {
        "name": "sre_repair",
        "description": "Execute only a repair the policy gate already approved. Client arguments cannot choose a runbook or a target.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
]


@dataclass
class McpSession:
    agent: OperationsAgent | None = None
    report: object | None = None
    messages: list[str] = field(default_factory=list)

    def operations(self) -> OperationsAgent:
        if self.agent is None:
            world = build_world()
            self.agent = OperationsAgent(world.scope, Registry(), world.clock, AgentConfig(mode="recommend"))
        return self.agent


def respond(session: McpSession, message: dict) -> dict | None:
    method = message.get("method", "")
    request_id = message.get("id")
    if method.startswith("notifications/") or request_id is None:
        return None
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sre-agent", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _tool_error(request_id, "Tool arguments must be an object.")
        try:
            text = _call(session, name)
        except KeyError:
            return _tool_error(request_id, f"Unknown tool {name}.")
        return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
    return _error(request_id, f"Unsupported method {method}.")


def _call(session: McpSession, name: str) -> str:
    if name == "sre_run_cycle":
        session.report = session.operations().run_cycle()
        return format_report(session.report)
    if name == "sre_explain":
        if session.report is None:
            session.report = session.operations().run_cycle()
        lines = []
        for note in session.report.narrations:
            suffix = f" Rejected model text: {note.rejected}" if note.rejected else ""
            lines.append(f"{note.incident_id} source={note.source} {note.text}{suffix}")
        return "\n".join(lines) or "No diagnosis was produced."
    if name == "sre_repair":
        if session.report is None:
            session.report = session.operations().run_cycle()
        approved = [item for item in session.report.decisions if item.outcome == "execute"]
        if not approved:
            return "Refused. The policy gate has no approved repair."
        actions = session.operations().execute(approved)
        return f"Executed {len(actions)} approved repair(s)."
    raise KeyError(name)


def serve(stdin=None, stdout=None) -> None:
    incoming = stdin if stdin is not None else sys.stdin.buffer
    outgoing = stdout if stdout is not None else sys.stdout.buffer
    session = McpSession()
    while True:
        message = read_message(incoming)
        if message is None:
            return
        reply = respond(session, message)
        if reply is not None:
            write_message(outgoing, reply)


def read_message(stream) -> dict | None:
    line = stream.readline()
    if not line:
        return None
    if line.lower().startswith(b"content-length:"):
        length = int(line.split(b":", 1)[1].strip())
        while True:
            header = stream.readline()
            if header in (b"\r\n", b"\n", b""):
                break
        payload = stream.read(length)
        return json.loads(payload.decode())
    text = line.decode().strip()
    if not text:
        return read_message(stream)
    return json.loads(text)


def write_message(stream, payload: dict) -> None:
    body = json.dumps(payload).encode()
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    stream.flush()


def _result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": message}}


def _tool_error(request_id, message: str) -> dict:
    return _result(request_id, {"content": [{"type": "text", "text": message}], "isError": True})
