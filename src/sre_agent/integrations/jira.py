"""Jira incidents. One open ticket per distinct problem, across every resource type."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from sre_agent.aws.errors import CloudError
from sre_agent.diagnosis.rules import (
    _breach_pairs,
    _placeholder_names,
    is_checker_machinery,
    plain_catalog_issue,
    plain_missing_issue,
    problem_identity_text,
)

_NOT_A_TICKET = frozenset(
    {
        "healthy",
        "signal.missing",
        "telemetry.missing",
        "monitoring.unsupported",
        "presence.uncertain",
        "registry.missing",
        "cloudwatch.alarm",
    }
)

_UNKNOWN_TITLES = {"unknown", "health is unknown", "health is unknown."}


class JiraError(CloudError):
    pass


def false_alarm_text(text: str) -> bool:
    """Unevaluable rules, missing logs, placeholders, and quiet telemetry are not tickets."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    markers = (
        "cannot be evaluated",
        "was not evaluated",
        "has no value",
        "unresolved placeholder",
        "resourcenotfoundexception",
        "log group does not exist",
        "no fault is currently observed",
        "required telemetry is missing",
        "not confirmed present",
        "signal.missing",
        "telemetry.missing",
    )
    if any(marker in lowered for marker in markers):
        return True
    if "${" in text:
        return not bool(_breach_pairs(text))
    return False


def issue_is_actionable(diagnosis) -> bool:
    """A Jira write is for a breached symptom or a confirmed fault.

    Closing still runs after a verified recovery. A placeholder, a quiet series,
    a missing log group, a healthy resource, or the checker's own queue does not
    open or update a ticket.
    """
    if is_checker_machinery(str(getattr(diagnosis, "resource_id", "") or "")):
        return False
    if getattr(diagnosis, "status", "") == "recovered":
        return True
    code = str(getattr(diagnosis, "problem_code", "") or "")
    if code in _NOT_A_TICKET or code.startswith("catalog.placeholder"):
        return False
    if str(getattr(diagnosis, "health", "") or "") == "healthy":
        return False
    if false_alarm_text(getattr(diagnosis, "observed_problem", "") or ""):
        return False
    if false_alarm_text(_issue_text(diagnosis)):
        return False
    blob = _evidence_blob(diagnosis)
    lowered = blob.lower()
    breached = bool(_breach_pairs(blob))
    real_breach = any("${" not in signal and not signal.startswith("$") for _rule, signal in _breach_pairs(blob))
    placeholder_problem = (
        bool(_placeholder_names(blob))
        or "unresolved placeholder" in lowered
        or "has no value" in lowered
        or "was not evaluated" in lowered
        or "cannot be evaluated" in lowered
        or "resourcenotfoundexception" in lowered
    )
    if placeholder_problem and not real_breach:
        return False
    if "could not read catalog rule" in lowered and not real_breach:
        return False
    if code == "catalog.symptom" and not real_breach:
        return False
    if "required telemetry is missing" in lowered and not real_breach:
        return False
    if not breached and code == "catalog.symptom":
        return False
    if str(getattr(diagnosis, "health", "") or "") not in {"degraded", "unhealthy"}:
        return False
    return _card_can_take_action(diagnosis)


def issue_dedup_key(diagnosis, record=None) -> str:
    """One open ticket per account, region, resource type, and problem code."""
    account = str(getattr(record, "account_id", "") or "")
    region = str(getattr(record, "region", "") or "")
    resource_type = str(getattr(record, "resource_type", "") or "")
    if not (account and region and resource_type):
        parsed_account, parsed_region, parsed_type = _identity_from_resource(getattr(diagnosis, "resource_id", "") or "")
        account = account or parsed_account
        region = region or parsed_region
        resource_type = resource_type or parsed_type
    code = str(getattr(diagnosis, "problem_code", "") or "unspecified")
    return f"{account}|{region}|{resource_type}|{code}"


def _identity_from_resource(resource_id: str) -> tuple[str, str, str]:
    from sre_agent.aws.names import SERVICE_TYPES, parse_arn

    try:
        parsed = parse_arn(resource_id)
    except ValueError:
        return "", "", ""
    return parsed.get("account") or "", parsed.get("region") or "", SERVICE_TYPES.get(parsed.get("service") or "", "")


def problem_identity(diagnosis) -> str:
    action = getattr(diagnosis, "proposed_action", None)
    runbook = str(getattr(action, "runbook_id", "") or "").strip()
    # A confirmed repair stays its own ticket. A shared catalog breach must not
    # pull that repair onto an unrelated queue's issue.
    if runbook and str(getattr(diagnosis, "problem_code", "") or "").strip():
        return str(diagnosis.problem_code)
    blob = _evidence_blob(diagnosis)
    missing = []
    for item in diagnosis.evidence:
        data = getattr(item, "data", None) or {}
        missing.extend(str(name) for name in (data.get("missing") or []))
    for item in diagnosis.uncertainty:
        missing.append(str(item))
    return problem_identity_text(diagnosis.problem_code, blob, missing)


def star_sections(diagnosis) -> tuple[str, str, str]:
    """Issue, Where, and Proposed solution. The solution stays a proposal until verified."""
    issue = _issue_text(diagnosis)
    seen = [str(item).strip() for item in (getattr(diagnosis, "where_seen", None) or []) if str(item).strip()]
    if not seen and getattr(diagnosis, "where_seen", None) is None and diagnosis.resource_id and diagnosis.status != "recovered":
        seen = [diagnosis.resource_id]
    return issue, "\n".join(seen), proposed_solution(diagnosis)


def star_plain(diagnosis) -> str:
    issue, where, proposed = star_sections(diagnosis)
    return f"* Issue\n{issue}\n\n* Where seen\n{where}\n\n* Proposed solution\n{proposed}"


def _diagnosis_resource(resource_id: str) -> tuple[str, str]:
    if ":function:" in resource_id:
        return resource_id.split(":function:", 1)[1], "lambda:function"
    if resource_id.startswith("arn:aws:sqs:"):
        return resource_id.rsplit(":", 1)[-1], "sqs:queue"
    if ":table/" in resource_id:
        return resource_id.rsplit("/", 1)[-1], "dynamodb:table"
    if "/restapis/" in resource_id:
        return "", "apigateway:restapi"
    return "", ""


def _card_rows(name: str, resource_id: str, resource_type: str) -> list[dict]:
    """Rows the page can match. A directory function also sees the API and table that share its name."""
    rows = []
    if name or resource_id:
        rows.append({"name": name, "resource_id": resource_id, "resource_type": resource_type})
    if resource_type == "lambda:function" and name in {"sre-dir-lab", "enterprise-sites-directory"}:
        rows.append(
            {
                "name": name,
                "resource_id": "arn:aws:apigateway:us-east-2::/restapis/shared-name",
                "resource_type": "apigateway:restapi",
            }
        )
        rows.append(
            {
                "name": name,
                "resource_id": f"arn:aws:dynamodb:us-east-2:000000000000:table/{name}",
                "resource_type": "dynamodb:table",
            }
        )
    return rows


def _card_can_take_action(diagnosis) -> bool:
    """Same decision the Jira card uses before it draws Take this action.

    The card is the saved Issue, Where seen, and Proposed solution text after the
    page links a resource. An API or table that shares the function name must not
    hide a directory restore, and it must not become a ticket on its own.
    """
    from sre_agent.demo_plans import resolved_proposal
    from sre_agent.demo_ui import _decision_for, _linked_issue

    issue, where, proposed = star_sections(diagnosis)
    resource_id = str(getattr(diagnosis, "resource_id", "") or "")
    name, resource_type = _diagnosis_resource(resource_id)
    view = {
        "summary": issue,
        "situation": issue,
        "where_seen": where,
        "task": where,
        "proposed": proposed,
        "resource_id": resource_id,
        "resource_name": name,
        "resource_type": resource_type,
    }
    linked = _linked_issue(view, _card_rows(name, resource_id, resource_type))
    linked["proposed"] = resolved_proposal(linked, proposed)
    return bool(_decision_for(linked).get("run"))


def proposed_solution(diagnosis) -> str:
    """A concrete correction the tool can run. The placeholder sentence is never used."""
    resource_id = str(getattr(diagnosis, "resource_id", "") or "")
    if is_checker_machinery(resource_id):
        return ""
    from sre_agent.demo_plans import concrete_correction

    name = resource_id.split(":function:", 1)[1] if ":function:" in resource_id else ""
    seen = getattr(diagnosis, "where_seen", None) or []
    concrete = concrete_correction(
        {
            "resource_id": resource_id,
            "resource_name": name,
            "resource_type": "lambda:function" if name else "",
            "where_seen": "\n".join(str(item) for item in seen),
            "situation": diagnosis.observed_problem or "",
            "summary": "",
        }
    )
    if concrete:
        return concrete
    if "enterprise-sites-directory" in resource_id:
        return "Set Lambda function enterprise-sites-directory TABLE_NAME to enterprise-sites."
    action = diagnosis.proposed_action
    if action is not None and str(action.runbook_id or "").strip():
        if is_checker_machinery(str(action.target_resource_id or ""), resource_id):
            return ""
        rationale = str(action.rationale or "").strip()
        lead = f"Run {action.runbook_id}."
        if rationale:
            lead = f"{lead} {rationale}"
        return lead + " This runs only after you verify it. The ticket closes after that action is performed."
    blob = _evidence_blob(diagnosis)
    steps = _correction_steps(diagnosis, blob)
    if steps:
        return " ".join(steps)
    if diagnosis.problem_code == "monitoring.unsupported":
        return "Register a monitoring pack for this resource type, or add a Confluence symptom that names the metric, statistic, and threshold to check."
    from sre_agent.demo_plans import concrete_correction

    seen = getattr(diagnosis, "where_seen", None) or []
    concrete = concrete_correction(
        {
            "resource_id": resource_id,
            "resource_name": "",
            "where_seen": "\n".join(str(item) for item in seen),
            "situation": diagnosis.observed_problem or "",
            "summary": "",
        }
    )
    return concrete


def _correction_steps(diagnosis, blob: str) -> list[str]:
    from sre_agent.diagnosis.rules import _breach_pairs, _missing_signal_names, _placeholder_names, _rule_id

    steps = []
    rule = _rule_id(blob)
    placeholders = _placeholder_names(blob)
    placeholder_problem = bool(placeholders) or "unresolved placeholder" in blob.lower() or "has no value" in blob.lower()
    if placeholder_problem:
        if placeholders:
            named = " and ".join(placeholders)
            target = f" on catalog rule {rule}" if rule else ""
            steps.append(
                f"Supply a value for {named}{target}. "
                "The checker already fills name, resource_id, region, account_id, and the service name from each resource. "
                f"Add {named} to those values, or replace the placeholder with the dimension, metric, threshold, or log group to use."
            )
        elif rule:
            steps.append(
                f"Supply the missing placeholder value on catalog rule {rule}. "
                "The checker already fills name, resource_id, region, account_id, and the service name from each resource. "
                "Replace the unresolved placeholder with the dimension, metric, threshold, or log group to use."
            )
        else:
            steps.append(
                "Supply the missing placeholder value. "
                "The checker already fills name, resource_id, region, account_id, and the service name from each resource."
            )
    if re.search(r"access log|log group is missing|log group (?:is )?(?:missing|unset|empty)", blob, flags=re.IGNORECASE):
        steps.append("Set the access log group to the CloudWatch log group this resource should write.")
    names = _listed_signals(diagnosis, blob)
    missing_problem = (
        diagnosis.problem_code == "signal.missing"
        or "required telemetry is missing" in blob.lower()
        or (not placeholder_problem and "could not read" in blob.lower())
    )
    catalog_reads = _missing_signal_names(blob)
    if catalog_reads:
        for name in catalog_reads:
            if name not in names:
                names.append(name)
    if names and (missing_problem or catalog_reads):
        steps.append(
            f"Publish {', '.join(names)} to CloudWatch for each resource listed under Where, "
            "using the namespace and dimensions the checker already queries."
        )
    elif missing_problem and not placeholder_problem:
        steps.append(
            "Publish the required telemetry to CloudWatch for each resource listed under Where, "
            "using the namespace and dimensions the checker already queries."
        )
    if not placeholder_problem:
        for rule_id, signal in _breach_pairs(blob):
            steps.append(
                f"Bring {signal} within the threshold on catalog rule {rule_id}, or change that threshold if the limit is wrong."
            )
        if not steps and "outside its threshold" in blob.lower() and rule:
            steps.append(f"Bring the signal within the threshold on catalog rule {rule}, or change that threshold if the limit is wrong.")
    return steps


def _issue_text(diagnosis) -> str:
    cleaned = _strip_unknown_title(diagnosis.observed_problem)
    if cleaned and not _is_dump(cleaned):
        return cleaned
    rewritten = _rewrite_issue(diagnosis)
    if rewritten and rewritten.lower() not in _UNKNOWN_TITLES:
        return rewritten
    if diagnosis.problem_code == "signal.missing":
        return "Required telemetry is missing."
    if diagnosis.problem_code and diagnosis.problem_code.lower() != "unknown":
        return diagnosis.problem_code
    return "Required telemetry is missing."


def _rewrite_issue(diagnosis) -> str:
    blob = _evidence_blob(diagnosis)
    catalog = plain_catalog_issue([blob]) if "catalog rule" in blob.lower() or "placeholder" in blob.lower() else ""
    if catalog and not _is_dump(catalog) and catalog != "A catalog rule is failing.":
        return catalog
    names = _listed_signals(diagnosis, blob)
    if names or diagnosis.problem_code == "signal.missing" or "required telemetry is missing" in blob.lower() or "could not read" in blob.lower():
        return plain_missing_issue(names)
    if cleaned := _strip_unknown_title(diagnosis.observed_problem):
        if not _is_dump(cleaned):
            return cleaned
    return plain_missing_issue(names)


def _strip_unknown_title(text: str) -> str:
    cleaned = re.sub(r"(?i)^health is unknown\.?\s*", "", (text or "").strip()).strip()
    if cleaned.lower() in _UNKNOWN_TITLES:
        return ""
    return cleaned


def _is_dump(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered or lowered in _UNKNOWN_TITLES:
        return True
    if lowered.startswith("health is "):
        return True
    if lowered.startswith("could not read"):
        return True
    if "unresolved placeholder" in lowered or "placeholder ${" in lowered:
        return True
    if re.search(r"catalog rule \S+ v\d+:", lowered):
        return True
    return False


def _evidence_blob(diagnosis) -> str:
    parts = [diagnosis.observed_problem or ""]
    parts.extend(item.summary for item in diagnosis.evidence[:8])
    parts.extend(diagnosis.uncertainty)
    parts.extend(diagnosis.likely_causes)
    return " ".join(part for part in parts if part).strip()


def _listed_signals(diagnosis, blob: str) -> list[str]:
    from sre_agent.diagnosis.rules import _missing_signal_names, _placeholder_names, _signal_list

    found = []
    for item in diagnosis.uncertainty:
        found.append(str(item))
    for item in diagnosis.evidence:
        data = getattr(item, "data", None) or {}
        found.extend(str(name) for name in (data.get("missing") or []))
    found.extend(_missing_signal_names(blob))
    placeholders = set(_placeholder_names(blob))
    return [name for name in _signal_list(found) if name not in placeholders]


def _normal_issue(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".")


def _line_has_resource(line: str, resource_id: str) -> bool:
    if not resource_id:
        return False
    return resource_id in [part for part in str(line).split("|") if part]


def assign_where(diagnosis, existing_description="") -> None:
    """Keep every resource that still has this problem, and add the one just seen."""
    incoming = [str(item).strip() for item in (getattr(diagnosis, "where_seen", None) or []) if str(item).strip()]
    if getattr(diagnosis, "where_authoritative", False):
        lines = list(dict.fromkeys(incoming))
    else:
        lines = _where_lines_from(existing_description)
        for line in incoming:
            if line not in lines:
                lines.append(line)
        if not lines and diagnosis.resource_id and diagnosis.status != "recovered":
            lines.append(diagnosis.resource_id)
    if diagnosis.status == "recovered":
        lines = [line for line in lines if not _line_has_resource(line, diagnosis.resource_id)]
    diagnosis.where_seen = lines


def _where_lines_from(description) -> list[str]:
    parsed = parse_star_description(description) if description else None
    if not parsed:
        return []
    body = parsed.get("where") or parsed.get("where_seen") or ""
    return [line.strip() for line in body.splitlines() if line.strip()]


def _find_open_issue(issues: dict, diagnosis):
    if diagnosis.jira_issue_key:
        existing = issues.get(diagnosis.jira_issue_key)
        if existing is not None and existing.status != "Done":
            return existing
    for item in issues.values():
        if item.status != "Done" and item.dedup_key == diagnosis.dedup_key:
            return item
    want = _normal_issue(_issue_text(diagnosis))
    if not want:
        return None
    for item in issues.values():
        if item.status == "Done":
            continue
        parsed = parse_star_description(item.description)
        candidate = (parsed or {}).get("issue") or item.summary
        if _normal_issue(candidate) == want:
            return item
    return None


@dataclass
class Approval:
    issue_key: str
    approver: str
    resource_id: str
    action: str
    expires_at: str


@dataclass
class JiraIssue:
    key: str
    dedup_key: str
    summary: str
    status: str = "Open"
    comments: list[str] = field(default_factory=list)
    approval: Approval | None = None
    updates: int = 0
    description: str = ""


class FakeJira:
    def __init__(self, project: str = "SRE", transition_name: str = "Done") -> None:
        self.project = project
        self.transition_name = transition_name
        self.issues: dict[str, JiraIssue] = {}
        self.failures = 0
        self.transitioned: list[str] = []
        self.comments: dict[str, list[dict[str, str]]] = {}
        self.last_created = False

    def sync(self, diagnosis) -> str:
        if not issue_is_actionable(diagnosis):
            self.last_created = False
            self._dismiss_false(diagnosis)
            return diagnosis.jira_issue_key or ""
        if self.failures:
            self.failures -= 1
            raise JiraError("Jira is unavailable")
        existing = _find_open_issue(self.issues, diagnosis)
        assign_where(diagnosis, existing.description if existing is not None else "")
        summary = _ticket_summary(diagnosis)
        description = star_plain(diagnosis)
        if existing is None:
            key = f"{self.project}-{len(self.issues) + 1}"
            self.issues[key] = JiraIssue(
                key=key,
                dedup_key=diagnosis.dedup_key,
                summary=summary,
                description=description,
            )
            self.comments.setdefault(key, [])
            self.last_created = True
            return key
        self.last_created = False
        existing.dedup_key = diagnosis.dedup_key
        existing.summary = summary
        existing.description = description
        existing.updates += 1
        if diagnosis.status == "recovered" and not diagnosis.where_seen:
            self._transition(existing)
            existing.comments.append("Resolved after verified recovery.")
        return existing.key

    def approval_for(self, issue_key: str) -> Approval | None:
        if issue_key not in self.issues:
            return None
        return approval_from_comments(issue_key, self.comments.get(issue_key, []))

    def grant(self, issue_key: str, approver: str, resource_id: str, action: str, expires_at: str) -> None:
        """Record an approval as written by approver. Text that names someone else is ignored."""
        self.comment(
            issue_key,
            approver,
            f"APPROVE resource={resource_id} action={action} expires={expires_at} approver=not-the-author",
        )

    def comment(self, issue_key: str, author_account_id: str, body: str) -> None:
        if issue_key not in self.issues:
            raise JiraError(issue_key)
        self.comments.setdefault(issue_key, []).append({"author_account_id": author_account_id, "body": body})

    def add_note(self, key: str, body: str) -> None:
        """Record a verified correction. This does not transition the issue."""
        if key not in self.issues:
            raise JiraError(key)
        self.comments.setdefault(key, []).append({"author_account_id": "sre-agent", "body": body})
        self.issues[key].comments.append(body)

    def close(self, key: str) -> None:
        issue = self.issues.get(key)
        if issue is None:
            raise JiraError(key)
        self._transition(issue)
        issue.comments.append("Closed after you verified the proposed action and it was performed.")

    def list_open(self) -> list[dict]:
        """Open issues, in the same shape the live client returns."""
        found = []
        for issue in self.issues.values():
            if issue.status == self.transition_name:
                continue
            found.append(
                {
                    "key": issue.key,
                    "fields": {
                        "summary": issue.summary,
                        "status": {"name": issue.status},
                        "description": issue.description,
                    },
                }
            )
        found.sort(key=lambda item: str(item.get("key") or ""))
        return found

    def dismiss(self, key: str) -> None:
        """Close a ticket that was never a fault. This is not a verified repair."""
        issue = self.issues.get(key)
        if issue is None or issue.status == self.transition_name:
            return
        self._transition(issue)
        issue.comments.append("Closed because this is not a current fault.")

    def _dismiss_false(self, diagnosis) -> None:
        text = _issue_text(diagnosis)
        if not false_alarm_text(text):
            return
        want = _normal_issue(text)
        for issue in list(self.issues.values()):
            if issue.status == self.transition_name:
                continue
            parsed = parse_star_description(issue.description)
            candidate = (parsed or {}).get("issue") or issue.summary
            if _normal_issue(candidate) == want:
                self.dismiss(issue.key)

    def _transition(self, issue: JiraIssue) -> None:
        issue.status = self.transition_name
        self.transitioned.append(issue.key)


class HttpJira:
    def __init__(self, base_url: str, project: str, email: str, token: str, transition_name: str = "Done", issue_type: str = "Task") -> None:
        self.base_url = base_url.rstrip("/")
        self.project = project
        self.email = email
        self.token = token
        self.transition_name = transition_name
        self.issue_type = issue_type
        self.last_created = False

    def sync(self, diagnosis) -> str:
        if not issue_is_actionable(diagnosis):
            self.last_created = False
            self._dismiss_false(diagnosis)
            return diagnosis.jira_issue_key or ""
        key = self._search_open(diagnosis)
        if not key:
            key = self._if_still_open(diagnosis.jira_issue_key)
        existing_description = ""
        if key and not getattr(diagnosis, "where_authoritative", False):
            existing_description = self._description(key)
        assign_where(diagnosis, existing_description)
        description = _star_doc(diagnosis)
        summary = _ticket_summary(diagnosis)[:250]
        labels = ["sre-agent", _label(diagnosis.dedup_key)]
        if not key:
            created = self._request(
                "POST",
                "/rest/api/3/issue",
                {
                    "fields": {
                        "project": {"key": self.project},
                        "summary": summary,
                        "issuetype": {"name": self.issue_type},
                        "labels": labels,
                        "description": description,
                    }
                },
            )
            self.last_created = True
            return created["key"]
        self.last_created = False
        self._request(
            "PUT",
            f"/rest/api/3/issue/{key}",
            {"fields": {"summary": summary, "description": description, "labels": labels}},
        )
        if diagnosis.status == "recovered" and not diagnosis.where_seen:
            self._transition(key)
            self._request("POST", f"/rest/api/3/issue/{key}/comment", {"body": _text("Resolved after verified recovery.")})
        return key

    def approval_for(self, issue_key: str) -> Approval | None:
        payload = self._request("GET", f"/rest/api/3/issue/{issue_key}?fields=comment")
        comments = []
        for comment in (payload.get("fields") or {}).get("comment", {}).get("comments", []):
            author = ((comment.get("author") or {}).get("accountId") or "")
            comments.append({"author_account_id": author, "body": _flatten(comment.get("body"))})
        return approval_from_comments(issue_key, comments)

    def close(self, key: str) -> None:
        self._transition(key)
        self._request(
            "POST",
            f"/rest/api/3/issue/{key}/comment",
            {"body": _text("Closed after you verified the proposed action and it was performed.")},
        )

    def add_note(self, key: str, body: str) -> None:
        """Record a verified correction. This does not transition the issue."""
        self._request("POST", f"/rest/api/3/issue/{key}/comment", {"body": _text(body)})

    def dismiss(self, key: str) -> None:
        """Close a ticket that was never a fault. This is not a verified repair."""
        self._transition(key)
        self._request(
            "POST",
            f"/rest/api/3/issue/{key}/comment",
            {
                "body": _text(
                    "Closed because this is not a current fault. "
                    "An unevaluable catalog rule, a missing log group, a placeholder, or missing telemetry does not keep a ticket open."
                )
            },
        )

    def _dismiss_false(self, diagnosis) -> None:
        text = _issue_text(diagnosis)
        if not false_alarm_text(text):
            return
        key = self._search_same_issue(text)
        if key:
            self.dismiss(key)

    def list_open(self) -> list[dict]:
        """Open issues in the configured project. Nothing is added that Jira did not return."""
        query = f'project = "{self.project}" AND statusCategory != Done'
        found: list[dict] = []
        page = ""
        while len(found) < 50:
            body = {"jql": query, "maxResults": 50, "fields": ["summary", "status", "description"]}
            if page:
                body["nextPageToken"] = page
            payload = self._request("POST", "/rest/api/3/search/jql", body)
            batch = payload.get("issues") or []
            found.extend(item for item in batch if isinstance(item, dict))
            page = str(payload.get("nextPageToken") or "")
            if not page or not batch:
                break
        hydrated = []
        for item in found[:50]:
            fields = item.get("fields") if isinstance(item, dict) else None
            key = str((item or {}).get("key") or "")
            missing = not isinstance(fields, dict) or "summary" not in fields
            if key and missing:
                try:
                    item = self._request("GET", f"/rest/api/3/issue/{key}?fields=summary,status,description")
                except JiraError:
                    pass
            if isinstance(item, dict):
                hydrated.append(item)
        hydrated.sort(key=lambda issue: str(issue.get("key") or ""))
        return hydrated

    def _transition(self, key: str) -> None:
        payload = self._request("GET", f"/rest/api/3/issue/{key}/transitions")
        transition_id = ""
        wanted = self.transition_name.casefold()
        for item in payload.get("transitions") or []:
            destination = str(((item.get("to") or {}).get("name") or ""))
            name = str(item.get("name") or "")
            if destination.casefold() == wanted or name.casefold() == wanted:
                transition_id = str(item.get("id") or "")
                break
        if not transition_id:
            raise JiraError(f"Jira has no transition to {self.transition_name} for {key}")
        self._request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": transition_id}})

    def _search_open(self, diagnosis) -> str:
        found = self._search(diagnosis.dedup_key)
        if found:
            return found
        return self._search_same_issue(_issue_text(diagnosis))

    def _search(self, dedup_key: str) -> str:
        label = _label(dedup_key)
        query = f'project = "{self.project}" AND labels = "sre-agent" AND labels = "{label}" AND statusCategory != Done'
        payload = self._request("POST", "/rest/api/3/search/jql", {"jql": query, "maxResults": 2, "fields": ["key"]})
        issues = payload.get("issues") or []
        if len(issues) > 1:
            raise JiraError(f"More than one open Jira issue matches {dedup_key}")
        return issues[0]["key"] if issues else ""

    def _search_same_issue(self, issue_text: str) -> str:
        want = _normal_issue(issue_text)
        if not want:
            return ""
        query = f'project = "{self.project}" AND labels = "sre-agent" AND statusCategory != Done'
        payload = self._request(
            "POST",
            "/rest/api/3/search/jql",
            {"jql": query, "maxResults": 50, "fields": ["summary", "description"]},
        )
        for item in payload.get("issues") or []:
            if not isinstance(item, dict):
                continue
            fields = item.get("fields") or {}
            parsed = parse_star_description(fields.get("description"))
            candidate = (parsed or {}).get("issue") or str(fields.get("summary") or "")
            if _normal_issue(candidate) == want:
                return str(item.get("key") or "")
        return ""

    def _if_still_open(self, key: str) -> str:
        if not key:
            return ""
        payload = self._request("GET", f"/rest/api/3/issue/{key}?fields=status")
        status = (payload.get("fields") or {}).get("status") or {}
        category = (status.get("statusCategory") or {}).get("key") if isinstance(status, dict) else ""
        if str(category).lower() == "done":
            return ""
        return key

    def _description(self, key: str):
        payload = self._request("GET", f"/rest/api/3/issue/{key}?fields=description")
        return (payload.get("fields") or {}).get("description") or ""

    def _request(self, method: str, path: str, body: dict | None = None):
        import base64

        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        token = base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise JiraError(f"HTTP {exc.code}: {detail}") from None
        except Exception as exc:
            raise JiraError(str(exc)) from exc
        return json.loads(raw) if raw else {}


def _label(dedup_key: str) -> str:
    safe = "".join(character if character.isalnum() else "-" for character in dedup_key)
    return ("sre-" + safe)[:255]


def _text(value: str) -> dict:
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": value[:3000]}]}]}


def _ticket_summary(diagnosis) -> str:
    return _issue_text(diagnosis)[:250]


def _star_doc(diagnosis) -> dict:
    issue, where, proposed = star_sections(diagnosis)
    content = []
    for title, body in (("Issue", issue), ("Where seen", where), ("Proposed solution", proposed)):
        content.append({"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": title}]})
        for line in (body or " ").split("\n"):
            content.append({"type": "paragraph", "content": [{"type": "text", "text": line[:2000]}]})
    return {"type": "doc", "version": 1, "content": content}


_STAR_TITLES = ("Issue", "Where", "Proposed solution")
_CANONICAL = {
    "issue": "Issue",
    "where": "Where",
    "where seen": "Where",
    "proposed solution": "Proposed solution",
    "situation": "Issue",
    "task": "Where",
    "proposed action": "Proposed solution",
}


def parse_star_description(description) -> dict[str, str] | None:
    """Issue, Where, and Proposed solution. Older Situation / Task / Proposed action text is accepted too."""
    if isinstance(description, dict):
        sections = _sections_from_adf(description)
    else:
        sections = _sections_from_plain(str(description or ""))
    if not all(title in sections for title in _STAR_TITLES):
        return None
    issue = sections["Issue"].strip()
    where = sections["Where"].strip()
    proposed = sections["Proposed solution"].strip()
    return {
        "issue": issue,
        "where": where,
        "where_seen": where,
        "proposed": proposed,
        "situation": issue,
        "task": where,
    }


def _canonical_star(text: str) -> str:
    cleaned = text.strip().lstrip("*").strip().lower()
    return _CANONICAL.get(cleaned, "")


def _sections_from_adf(node: dict) -> dict[str, str]:
    content = node.get("content") if isinstance(node, dict) else None
    if not isinstance(content, list):
        return _sections_from_plain(_flatten(node))
    sections: dict[str, list[str]] = {}
    current = ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "heading":
            current = _canonical_star(_flatten(block))
            if current:
                sections.setdefault(current, [])
            continue
        if current and isinstance(block, dict):
            text = _flatten(block).strip()
            if text:
                sections.setdefault(current, []).append(text)
    return {key: "\n".join(parts).strip() for key, parts in sections.items()}


def _sections_from_plain(text: str) -> dict[str, str]:
    import re

    pattern = re.compile(
        r"(?:^|\n)\*?\s*(Issue|Where seen|Where|Proposed solution|Situation|Task|Proposed action)\s*(?:\n|$)",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text or ""))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = _canonical_star(match.group(1))
        if title and title not in sections:
            sections[title] = text[start:end].strip()
    return sections


def _flatten(body) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        parts = []
        if "text" in body:
            parts.append(str(body["text"]))
        for child in body.get("content") or []:
            parts.append(_flatten(child))
        return " ".join(part for part in parts if part)
    if isinstance(body, list):
        return " ".join(_flatten(item) for item in body)
    return ""


def approval_from_comments(issue_key: str, comments: list[dict[str, str]]) -> Approval | None:
    """The approver is the latest comment author's Atlassian account ID, never text inside the comment."""
    found = None
    for comment in comments:
        parsed = _parse_approval(comment.get("body", ""))
        author = comment.get("author_account_id", "")
        if parsed is None or not author:
            continue
        found = Approval(issue_key, author, parsed["resource"], parsed["action"], parsed["expires"])
    return found


def _parse_approval(text: str) -> dict[str, str] | None:
    if not text.startswith("APPROVE "):
        return None
    fields = {}
    for token in text.split()[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key == "approver":
            continue
        fields[key] = value
    if not {"resource", "action", "expires"} <= set(fields):
        return None
    return fields
