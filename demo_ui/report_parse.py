"""Parse the text report printed by python -m sre_agent."""

from __future__ import annotations

import re

_SCAN = re.compile(
    r"^  (\S+)\s+(\S+)\s+(success|partial|error|denied|not-configured|unmapped)\s+(\d+)\s+(.*)$"
)
_RESOURCE = re.compile(
    r"^  (\S+)\s+(.+?)\s+(present|uncertain|deleted)\s+(\S+)\s+(\S+)\s+owner=(\S*)(?:\s+source=(\S+))?\s*$"
)
_DIAGNOSIS = re.compile(
    r"^  (\S+)\s+(\S+)\s+action=(\S+)(?:\s+resource=(\S+)\s+jira=(\S+))?\s*$"
)
_LAB = re.compile(r"^\s+(PASS|FAIL)\s+(.+?):\s*(.*)$")


def parse_report(text: str) -> dict:
    """Read the sections format_report prints. Extra source and jira fields are optional."""
    section = ""
    scans = []
    resources = []
    diagnoses = []
    pending = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line in {
            "Discovery and reconciliation",
            "Registry",
            "Diagnosis",
            "Policy gate",
            "Executor and verification",
            "Agents",
        }:
            section = line
            pending = None
            continue
        if section == "Discovery and reconciliation":
            match = _SCAN.match(line)
            if match:
                scans.append(
                    {
                        "collector": match.group(1),
                        "region": match.group(2),
                        "status": match.group(3),
                        "count": int(match.group(4)),
                        "message": match.group(5).strip(),
                    }
                )
        elif section == "Registry":
            match = _RESOURCE.match(line)
            if match:
                resources.append(
                    {
                        "resource_type": match.group(1),
                        "name": match.group(2).strip(),
                        "presence": match.group(3),
                        "monitoring_status": match.group(4),
                        "health": match.group(5),
                        "owner": match.group(6),
                        "source": match.group(7) or "",
                    }
                )
        elif section == "Diagnosis":
            match = _DIAGNOSIS.match(line)
            if match:
                pending = {
                    "status": match.group(1),
                    "problem_code": match.group(2),
                    "action": match.group(3),
                    "resource": match.group(4) or "",
                    "jira": "" if match.group(5) in (None, "-") else match.group(5),
                    "problem": "",
                }
                diagnoses.append(pending)
                continue
            if pending is not None and line.startswith("    "):
                pending["problem"] = line.strip()
                pending = None
    return {"scans": scans, "resources": resources, "diagnoses": diagnoses}


def parse_lab(text: str) -> dict:
    checks = []
    for line in text.splitlines():
        match = _LAB.match(line)
        if match:
            checks.append({"mark": match.group(1), "name": match.group(2), "detail": match.group(3).strip()})
    return {"checks": checks, "passed": all(item["mark"] == "PASS" for item in checks) and bool(checks)}


def resource_named(report: dict, name: str, resource_type: str = "") -> dict | None:
    for item in report["resources"]:
        if item["name"] == name and (not resource_type or item["resource_type"] == resource_type):
            return item
    return None


def diagnoses_for(report: dict, name: str) -> list[dict]:
    return [item for item in report["diagnoses"] if item["resource"] == name]
