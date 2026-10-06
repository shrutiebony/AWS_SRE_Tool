"""Confluence catalog: read a page, validate the rule table, and keep its version."""

from __future__ import annotations

import html
import json
import re
import urllib.request
from dataclasses import dataclass

from sre_agent.aws.errors import CloudError

REQUIRED_COLUMNS = ("resource_type", "signal", "statistic", "comparison", "threshold")
SYMPTOM_COLUMNS = ("Symptom ID", "Metric / query", "Condition", "Threshold", "Status")
_NAMESPACE_TYPES = {
    "AWS/SQS": "sqs:queue",
    "AWS/Lambda": "lambda:function",
    "AWS/DynamoDB": "dynamodb:table",
    "AWS/ApiGateway": "apigateway:restapi",
}
_SERVICE_TYPES = {
    "SQS": "sqs:queue",
    "Lambda": "lambda:function",
    "DynamoDB": "dynamodb:table",
    "API Gateway": "apigateway:restapi",
}
_CONDITIONS = {
    ">": "GreaterThanThreshold",
    ">=": "GreaterThanOrEqualToThreshold",
    "<": "LessThanThreshold",
}
_STATS = {"Sum": "Sum", "Average": "Average", "Maximum": "Maximum", "Minimum": "Minimum", "Count": "Count"}
COMPARISONS = {"GreaterThanThreshold", "GreaterThanOrEqualToThreshold", "LessThanThreshold"}
STATISTICS = {"Sum", "Average", "Maximum", "Minimum", "Expression", "Count"}


@dataclass
class CatalogRule:
    rule_id: str
    version: int
    resource_type: str
    signal: str
    statistic: str
    comparison: str
    threshold: float
    expression: str = ""
    log_query: str = ""
    period_seconds: int = 300
    role: str = "primary"
    threshold_expression: str = ""

    def resolved(self, values: dict[str, str]) -> "CatalogRule":
        return CatalogRule(
            rule_id=self.rule_id,
            version=self.version,
            resource_type=self.resource_type,
            signal=_fill(self.signal, values),
            statistic=self.statistic,
            comparison=self.comparison,
            threshold=self.threshold,
            expression=_fill(self.expression, values),
            log_query=_fill(self.log_query, values),
            period_seconds=self.period_seconds,
            role=self.role,
            threshold_expression=_fill(self.threshold_expression, values),
        )


@dataclass
class Catalog:
    version: int
    page_id: str
    rules: list[CatalogRule]
    status: str = "loaded"
    message: str = ""


class ConfluenceError(CloudError):
    pass


class FakeConfluence:
    def __init__(self, storage: str, version: int = 1, page_id: str = "100") -> None:
        self.storage = storage
        self.version = version
        self.page_id = page_id
        self.failures = 0

    def fetch_page(self) -> tuple[int, str]:
        if self.failures:
            self.failures -= 1
            raise ConfluenceError("Confluence page could not be read")
        return self.version, self.storage


class HttpConfluence:
    def __init__(self, base_url: str, page_id: str, email: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_id = page_id
        self.email = email
        self.token = token

    def fetch_page(self) -> tuple[int, str]:
        url = f"{self.base_url}/wiki/api/v2/pages/{self.page_id}?body-format=storage"
        request = urllib.request.Request(url)
        token = f"{self.email}:{self.token}".encode()
        request.add_header("Authorization", "Basic " + _b64(token))
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode())
        except Exception as exc:
            raise ConfluenceError(str(exc)) from exc
        version = int((payload.get("version") or {}).get("number") or 0)
        storage = ((payload.get("body") or {}).get("storage") or {}).get("value") or ""
        return version, storage


def load_catalog(client, page_id: str = "") -> Catalog:
    version, storage = client.fetch_page()
    rules = parse_rule_table(storage, version)
    errors = validate_rules(rules)
    if errors:
        raise ConfluenceError("; ".join(errors))
    return Catalog(version=version, page_id=page_id or getattr(client, "page_id", ""), rules=rules)


def parse_rule_table(storage: str, version: int) -> list[CatalogRule]:
    rules = _rules_in_order(storage, version)
    if rules:
        return rules
    rows = _table_rows(storage)
    if not rows:
        raise ConfluenceError("The Confluence page has no rule table")
    return _rules_from_rows(rows, version, "")


def _rules_in_order(storage: str, version: int) -> list[CatalogRule]:
    rules: list[CatalogRule] = []
    service = ""
    for heading, table in re.findall(r"<h2\b[^>]*>(.*?)</h2>|<table\b[^>]*>(.*?)</table>", storage, flags=re.IGNORECASE | re.DOTALL):
        if heading:
            text = _clean(heading)
            if text.lower().startswith("service:"):
                service = text.split(":", 1)[1].strip()
            continue
        rules.extend(_rules_from_rows(_rows(table), version, service))
    return rules


def _rules_from_rows(rows: list[list[str]], version: int, service: str) -> list[CatalogRule]:
    if not rows:
        return []
    header = [_clean(cell) for cell in rows[0]]
    if all(name in header for name in REQUIRED_COLUMNS):
        return _legacy_rules(rows, header, version)
    if all(name in header for name in SYMPTOM_COLUMNS):
        return _symptom_rules(rows, header, version, service)
    return []


def _legacy_rules(rows: list[list[str]], header: list[str], version: int) -> list[CatalogRule]:
    index = {name: header.index(name) for name in header}
    rules = []
    for number, row in enumerate(rows[1:], start=1):
        if not any(_clean(cell) for cell in row):
            continue
        values = {name: _clean(row[position]) if position < len(row) else "" for name, position in index.items()}
        rules.append(
            CatalogRule(
                rule_id=values.get("rule_id") or f"rule-{number}",
                version=version,
                resource_type=values["resource_type"],
                signal=values["signal"],
                statistic=values["statistic"],
                comparison=values["comparison"],
                threshold=float(values["threshold"]),
                expression=values.get("expression", ""),
                log_query=values.get("log_query", ""),
                period_seconds=int(values.get("period_seconds") or 300),
            )
        )
    return rules


def _symptom_rules(rows: list[list[str]], header: list[str], version: int, service: str) -> list[CatalogRule]:
    index = {name: header.index(name) for name in header}
    rules = []
    for row in rows[1:]:
        if not any(_clean(cell) for cell in row):
            continue
        values = {name: _clean(row[position]) if position < len(row) else "" for name, position in index.items()}
        if not values["Status"].lower().startswith("active"):
            continue
        kind = values.get("Type", "metric")
        namespace = values.get("Namespace / log group", "")
        resource_type = _NAMESPACE_TYPES.get(namespace) or _SERVICE_TYPES.get(service, "")
        metric = values["Metric / query"]
        statistic = _STATS.get(values.get("Stat", ""), "Count")
        expression = ""
        log_query = ""
        signal = metric
        if kind == "metric_math":
            statistic = "Expression"
            expression = metric
        elif kind == "log":
            statistic = "Count"
            log_query = metric
            signal = "value"
        comparison = _CONDITIONS.get(values["Condition"])
        if comparison is None or not resource_type:
            raise ConfluenceError(f"{values['Symptom ID']} has a condition or service the catalog does not understand")
        threshold, threshold_expression = _threshold(values["Threshold"])
        window = int(values.get("Window (min)") or 5)
        rules.append(
            CatalogRule(
                rule_id=values["Symptom ID"],
                version=version,
                resource_type=resource_type,
                signal=signal,
                statistic=statistic,
                comparison=comparison,
                threshold=threshold,
                expression=expression,
                log_query=log_query,
                period_seconds=window * 60,
                role="supporting" if values.get("Role", "").lower() == "supporting" else "primary",
                threshold_expression=threshold_expression,
            )
        )
    return rules


def _threshold(value: str) -> tuple[float, str]:
    if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        return float(value), ""
    return 0.0, value


def validate_rules(rules: list[CatalogRule]) -> list[str]:
    errors = []
    seen = set()
    for rule in rules:
        if rule.rule_id in seen:
            errors.append(f"{rule.rule_id} is duplicated")
        seen.add(rule.rule_id)
        if not rule.resource_type or not rule.signal:
            errors.append(f"{rule.rule_id} needs a resource type and a signal")
        if rule.statistic not in STATISTICS:
            errors.append(f"{rule.rule_id} has statistic {rule.statistic}")
        if rule.comparison not in COMPARISONS:
            errors.append(f"{rule.rule_id} has comparison {rule.comparison}")
        if rule.statistic == "Expression" and not rule.expression:
            errors.append(f"{rule.rule_id} is metric math without an expression")
    return errors


def _table_rows(storage: str) -> list[list[str]]:
    """Use the table whose header has the rule columns, not whichever table appears first."""
    tables = re.findall(r"<table\b.*?>(.*?)</table>", storage, flags=re.IGNORECASE | re.DOTALL)
    parsed = [_rows(table) for table in tables]
    for rows in parsed:
        if not rows:
            continue
        header = [_clean(cell) for cell in rows[0]]
        if all(name in header for name in REQUIRED_COLUMNS):
            return rows
    return parsed[0] if parsed else []


def _rows(table_html: str) -> list[list[str]]:
    rows = []
    for row_html in re.findall(r"<tr\b.*?>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<t[dh]\b.*?>(.*?)</t[dh]>", row_html, flags=re.IGNORECASE | re.DOTALL)
        rows.append(cells)
    return rows


def _clean(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _fill(template: str, values: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise ConfluenceError(f"Placeholder ${{{key}}} has no value")
        return values[key]

    filled = re.sub(r"\$\{([A-Za-z0-9_]+)\}", replace, template)
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, filled)


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode()
