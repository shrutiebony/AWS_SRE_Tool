"""Text report for one operations cycle."""

from __future__ import annotations

from sre_agent.service import CycleReport


def format_report(report: CycleReport) -> str:
    lines = ["Discovery and reconciliation"]
    for scan in report.scans:
        if scan.collector == "reconciliation":
            continue
        lines.append(
            f"  {scan.collector:<22} {scan.region:<8} {scan.status:<16} {scan.resource_count:>3}  {scan.message}"
        )
    lines.append("")
    lines.append("Registry")
    for record in sorted(report.resources, key=lambda item: (item.resource_type, item.name)):
        view = record.public_view()
        owner = record.ownership.get("owner") or record.metadata.get("owner") or "-"
        lines.append(
            f"  {view['resource_type']:<16} {record.name:<18} {record.presence:<10} "
            f"{view['monitoring_status']:<16} {record.health:<10} owner={owner}"
        )
    lines.append("")
    lines.append("Diagnosis")
    if not report.diagnoses:
        lines.append("  none")
    for diagnosis in report.diagnoses:
        action = diagnosis.proposed_action.runbook_id if diagnosis.proposed_action else "none"
        lines.append(f"  {diagnosis.status:<14} {diagnosis.problem_code:<28} action={action}")
        lines.append(f"    {diagnosis.observed_problem}")
    lines.append("")
    lines.append("Policy gate")
    if not report.decisions:
        lines.append("  none")
    for decision in report.decisions:
        if decision.outcome == "no-action":
            continue
        lines.append(f"  {decision.outcome:<12} {decision.runbook_id or '-':<18} {'; '.join(decision.reasons)}")
    lines.append("")
    lines.append("Executor and verification")
    if not report.actions:
        lines.append("  no repairs were executed")
    for action, verification in _pairs(report):
        lines.append(
            f"  {action.runbook_id} api={action.status} verification={verification.status} rollback={verification.rollback}"
        )
        lines.append(f"    {verification.reasons[0] if verification.reasons else ''}")
    return "\n".join(lines)


def _pairs(report: CycleReport):
    by_action = {item.action_id: item for item in report.verifications}
    for action in report.actions:
        if action.action_id in by_action:
            yield action, by_action[action.action_id]
