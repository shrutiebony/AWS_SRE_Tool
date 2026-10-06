"""Jira incidents. One open issue per account, Region, resource, and problem type."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field

from sre_agent.aws.errors import CloudError


class JiraError(CloudError):
    pass


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


class FakeJira:
    def __init__(self, project: str = "SRE", transition_name: str = "Done") -> None:
        self.project = project
        self.transition_name = transition_name
        self.issues: dict[str, JiraIssue] = {}
        self.failures = 0
        self.transitioned: list[str] = []
        self.comments: dict[str, list[dict[str, str]]] = {}

    def sync(self, diagnosis) -> str:
        if self.failures:
            self.failures -= 1
            raise JiraError("Jira is unavailable")
        existing = self.issues.get(diagnosis.jira_issue_key) if diagnosis.jira_issue_key else None
        if existing is None:
            existing = next((item for item in self.issues.values() if item.dedup_key == diagnosis.dedup_key and item.status != "Done"), None)
        evidence = "; ".join(item.summary for item in diagnosis.evidence[:5]) or diagnosis.observed_problem
        if existing is None:
            key = f"{self.project}-{len(self.issues) + 1}"
            self.issues[key] = JiraIssue(key=key, dedup_key=diagnosis.dedup_key, summary=diagnosis.observed_problem, comments=[evidence])
            self.comments.setdefault(key, [])
            return key
        existing.summary = diagnosis.observed_problem
        existing.updates += 1
        existing.comments.append(evidence)
        if diagnosis.status == "recovered":
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

    def sync(self, diagnosis) -> str:
        key = diagnosis.jira_issue_key or self._search(diagnosis.dedup_key)
        evidence = "; ".join(item.summary for item in diagnosis.evidence[:5]) or diagnosis.observed_problem
        if not key:
            created = self._request(
                "POST",
                "/rest/api/3/issue",
                {
                    "fields": {
                        "project": {"key": self.project},
                        "summary": diagnosis.observed_problem[:250],
                        "issuetype": {"name": self.issue_type},
                        "labels": ["sre-agent", _label(diagnosis.dedup_key)],
                        "description": _text(evidence),
                    }
                },
            )
            return created["key"]
        self._request("POST", f"/rest/api/3/issue/{key}/comment", {"body": _text(evidence)})
        if diagnosis.status == "recovered":
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

    def _transition(self, key: str) -> None:
        payload = self._request("GET", f"/rest/api/3/issue/{key}/transitions")
        transition_id = ""
        for item in payload.get("transitions") or []:
            destination = ((item.get("to") or {}).get("name") or "")
            if destination == self.transition_name or item.get("name") == self.transition_name:
                transition_id = str(item.get("id") or "")
                break
        if not transition_id:
            raise JiraError(f"Jira has no transition to {self.transition_name} for {key}")
        self._request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": transition_id}})

    def _search(self, dedup_key: str) -> str:
        label = _label(dedup_key)
        query = f'project = "{self.project}" AND labels = "sre-agent" AND labels = "{label}" AND statusCategory != Done'
        payload = self._request("POST", "/rest/api/3/search/jql", {"jql": query, "maxResults": 2, "fields": ["key"]})
        issues = payload.get("issues") or []
        if len(issues) > 1:
            raise JiraError(f"More than one open Jira issue matches {dedup_key}")
        return issues[0]["key"] if issues else ""

    def _request(self, method: str, path: str, body: dict | None = None):
        import base64

        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        token = base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode()
        except Exception as exc:
            raise JiraError(str(exc)) from exc
        return json.loads(raw) if raw else {}


def _label(dedup_key: str) -> str:
    safe = "".join(character if character.isalnum() else "-" for character in dedup_key)
    return ("sre-" + safe)[:255]


def _text(value: str) -> dict:
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": value[:3000]}]}]}


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
