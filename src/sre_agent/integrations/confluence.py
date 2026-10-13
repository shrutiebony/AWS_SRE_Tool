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
    namespace: str = ""
    dimension: str = ""

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
            namespace=_fill(self.namespace, values),
            dimension=_fill(self.dimension, values),
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
        version, _title, storage, _space = self.fetch_document()
        return version, storage

    def fetch_document(self) -> tuple[int, str, str, str]:
        payload = self._request("GET", f"/wiki/api/v2/pages/{self.page_id}?body-format=storage")
        version = int((payload.get("version") or {}).get("number") or 0)
        title = str(payload.get("title") or "")
        storage = ((payload.get("body") or {}).get("storage") or {}).get("value") or ""
        space_id = str(payload.get("spaceId") or "")
        return version, title, storage, space_id

    def write_storage(self, title: str, storage: str, version: int, space_id: str = "") -> None:
        """Append-only callers pass the previous storage plus the new section."""
        body = {
            "id": str(self.page_id),
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": storage},
            "version": {"number": int(version) + 1, "message": "Add a service section"},
        }
        if space_id:
            body["spaceId"] = space_id
        self._request("PUT", f"/wiki/api/v2/pages/{self.page_id}", body)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        token = f"{self.email}:{self.token}".encode()
        request.add_header("Authorization", "Basic " + _b64(token))
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode()
        except Exception as exc:
            raise ConfluenceError(str(exc)) from exc
        return json.loads(raw) if raw else {}


def load_catalog(client, page_id: str = "", services: list | None = None) -> Catalog:
    version, storage = client.fetch_page()
    rules = parse_rule_table(storage, version, services)
    errors = validate_rules(rules)
    if errors:
        raise ConfluenceError("; ".join(errors))
    return Catalog(version=version, page_id=page_id or getattr(client, "page_id", ""), rules=rules)


def parse_rule_table(storage: str, version: int, services: list | None = None) -> list[CatalogRule]:
    rules = _rules_in_order(storage, version, services)
    if rules:
        return rules
    rows = _table_rows(storage)
    if not rows:
        raise ConfluenceError("The Confluence page has no rule table")
    return _rules_from_rows(rows, version, "", _type_map(services))


def _rules_in_order(storage: str, version: int, services: list | None = None) -> list[CatalogRule]:
    rules: list[CatalogRule] = []
    service = ""
    explicit_type = ""
    type_map = _type_map(services)
    for heading, table in re.findall(r"<h2\b[^>]*>(.*?)</h2>|<table\b[^>]*>(.*?)</table>", storage, flags=re.IGNORECASE | re.DOTALL):
        if heading:
            text = _clean(heading)
            if text.lower().startswith("service:"):
                service, explicit_type = _service_heading(text.split(":", 1)[1].strip())
            continue
        rules.extend(_rules_from_rows(_rows(table), version, service, type_map, explicit_type))
    return rules


def _type_map(services: list | None) -> dict[str, str]:
    mapped = {}
    for item in services or []:
        service = getattr(item, "service", "")
        resource_type = getattr(item, "resource_type", "")
        if service and resource_type:
            mapped[str(service).strip().lower()] = resource_type
    return mapped


def _service_heading(text: str) -> tuple[str, str]:
    match = re.fullmatch(r"(.+?)\s*\(([a-z0-9]+:[a-z0-9_-]+)\)", text.strip(), flags=re.IGNORECASE)
    if match:
        return match.group(1).strip(), match.group(2)
    if re.fullmatch(r"[a-z0-9]+:[a-z0-9_-]+", text.strip(), flags=re.IGNORECASE):
        return text.strip(), text.strip()
    return text.strip(), ""


def _rules_from_rows(
    rows: list[list[str]],
    version: int,
    service: str,
    type_map: dict[str, str] | None = None,
    explicit_type: str = "",
) -> list[CatalogRule]:
    if not rows:
        return []
    header = [_clean(cell) for cell in rows[0]]
    if all(name in header for name in REQUIRED_COLUMNS):
        return _legacy_rules(rows, header, version)
    if all(name in header for name in SYMPTOM_COLUMNS):
        return _symptom_rules(rows, header, version, service, type_map or {}, explicit_type)
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


def _symptom_rules(
    rows: list[list[str]],
    header: list[str],
    version: int,
    service: str,
    type_map: dict[str, str],
    explicit_type: str,
) -> list[CatalogRule]:
    index = {name: header.index(name) for name in header}
    rules = []
    for row in rows[1:]:
        if not any(_clean(cell) for cell in row):
            continue
        values = {name: _clean(row[position]) if position < len(row) else "" for name, position in index.items()}
        if not re.search(r"\bactive\b", values["Status"].lower()):
            continue
        kind = values.get("Type", "metric")
        namespace = values.get("Namespace / log group", "")
        row_type = values.get("Resource type", "")
        resource_type = (
            _NAMESPACE_TYPES.get(namespace)
            or _SERVICE_TYPES.get(service, "")
            or row_type
            or explicit_type
            or type_map.get(service.strip().lower(), "")
        )
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
                namespace=namespace,
                dimension=values.get("Dimension", ""),
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


_VIEW_FIELDS = (
    ("symptom", "Symptom ID"),
    ("measure", "Metric / query"),
    ("condition", "Condition"),
    ("threshold", "Threshold"),
    ("status", "Status"),
    ("kind", "Type"),
    ("role", "Role"),
)


def catalog_sections(storage: str) -> list[dict]:
    """Service headings and symptom rows already stored on the page.

    This only reads storage. It does not validate placeholders or write the page.
    Empty fields, unresolved placeholders, and scan errors are left out.
    """
    sections: list[dict] = []
    current: dict | None = None
    pattern = re.compile(r"<h2\b[^>]*>(.*?)</h2>|<table\b[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)
    for heading, table in pattern.findall(storage or ""):
        if heading:
            text = _clean(heading)
            if text.lower().startswith("service:") and not _hidden_catalog_text(text):
                current = {"heading": text, "rows": []}
                sections.append(current)
            else:
                current = None
            continue
        if current is None:
            continue
        parsed = _rows(table)
        if len(parsed) < 2:
            continue
        header = [_clean(cell) for cell in parsed[0]]
        needed = ("Symptom ID", "Metric / query", "Condition", "Threshold", "Status")
        if not all(name in header for name in needed):
            continue
        index = {name: header.index(name) for name in header}
        for raw in parsed[1:]:
            values = {name: _clean(raw[position]) if position < len(raw) else "" for name, position in index.items()}
            row = _visible_symptom(values)
            if row:
                current["rows"].append(row)
    return sections


def _visible_symptom(values: dict) -> dict:
    shown = {}
    for key, column in _VIEW_FIELDS:
        text = str(values.get(column) or "").strip()
        if not text or _hidden_catalog_text(text):
            continue
        shown[key] = text
    if "symptom" not in shown:
        return {}
    return shown


def _hidden_catalog_text(text: str) -> bool:
    lowered = text.lower()
    return "${" in text or "was not evaluated" in lowered or "resourcenotfoundexception" in lowered


def _fill(template: str, values: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise ConfluenceError(f"Placeholder ${{{key}}} has no value")
        return values[key]

    filled = re.sub(r"\$\{([A-Za-z0-9_]+)\}", replace, template)
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, filled)


WORKLOAD_COLUMNS = (
    "Symptom ID",
    "Scenario IDs",
    "Type",
    "Namespace / log group",
    "Dimension",
    "Metric / query",
    "Stat",
    "Condition",
    "Threshold",
    "Window (min)",
    "Role",
    "Status",
)
_CODE_COLUMNS = {"Namespace / log group", "Dimension", "Metric / query", "Threshold"}
_WORKLOAD_CONDITIONS = {">", ">=", "<"}
_WORKLOAD_KINDS = {"metric", "log"}
_WORKLOAD_ROLES = {"primary", "supporting"}
_WORKLOAD_STATS = {"Sum", "Average", "Maximum", "Minimum", "Count"}


def service_heading(service: str, resource_type: str) -> str:
    return f"Service: {service} ({resource_type})"


def storage_has_service(storage: str, service: str, resource_type: str) -> bool:
    target = service_heading(service, resource_type).lower()
    for heading in re.findall(r"<h2\b[^>]*>(.*?)</h2>", storage or "", flags=re.IGNORECASE | re.DOTALL):
        if _clean(heading).lower() == target:
            return True
    return False


def validate_workload_fields(fields: dict) -> str:
    """Return an error sentence, or empty when the form can be written."""
    service = str(fields.get("service") or "").strip()
    resource_type = str(fields.get("resource_type") or "").strip()
    measure = str(fields.get("measure") or "").strip()
    dimension = str(fields.get("dimension") or "").strip()
    stat = str(fields.get("stat") or "").strip()
    window = str(fields.get("window") or "").strip()
    role = str(fields.get("role") or "").strip()
    if not service:
        return "Service name is required."
    if not re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_-]+", resource_type):
        return "Resource type must look like eks:cluster."
    if not measure:
        return "What to measure is required."
    if "=" not in dimension:
        return "Dimension must look like Name=Value."
    if stat not in _WORKLOAD_STATS:
        return "Stat must be Sum, Average, Maximum, Minimum, or Count."
    if not re.fullmatch(r"[1-9][0-9]*", window):
        return "Window in minutes must be a positive whole number."
    if role not in _WORKLOAD_ROLES:
        return "Role must be primary or supporting."
    rows = _condition_rows(fields)
    if not rows:
        return "At least one condition is required."
    for number, row in enumerate(rows, start=1):
        error = _condition_error(row)
        if error:
            return f"Condition {number}: {error}"
    return ""


def workload_section(fields: dict, storage: str) -> str:
    """One service heading and one symptom table, in the page's storage format."""
    service = str(fields.get("service") or "").strip()
    resource_type = str(fields.get("resource_type") or "").strip()
    measure = str(fields.get("measure") or "").strip()
    dimension = str(fields.get("dimension") or "").strip()
    stat = str(fields.get("stat") or "").strip()
    window = str(fields.get("window") or "").strip()
    role = str(fields.get("role") or "").strip()
    rows = _condition_rows(fields)
    given = str(fields.get("symptom_id") or "").strip()
    seen = storage or ""
    body_rows = []
    for index, row in enumerate(rows):
        if index == 0 and given:
            symptom = given
        else:
            symptom = _next_symptom_id(service, seen)
        seen += "\n" + symptom
        values = {
            "Symptom ID": symptom,
            "Scenario IDs": "",
            "Type": str(row.get("kind") or "").strip(),
            "Namespace / log group": str(row.get("namespace") or "").strip(),
            "Dimension": dimension,
            "Metric / query": measure,
            "Stat": stat,
            "Condition": str(row.get("condition") or "").strip(),
            "Threshold": str(row.get("threshold") or "").strip(),
            "Window (min)": window,
            "Role": role,
        }
        cells = "".join(_storage_cell(name, values[name]) for name in WORKLOAD_COLUMNS if name != "Status")
        cells += _status_cell()
        body_rows.append(f"<tr>{cells}</tr>")
    header = "".join(f"<th><p>{html.escape(name)}</p></th>" for name in WORKLOAD_COLUMNS)
    heading = html.escape(service_heading(service, resource_type))
    table = (
        '<table data-table-width="1800" data-layout="full-width"><tbody>'
        f"<tr>{header}</tr>{''.join(body_rows)}</tbody></table>"
    )
    return f"<h2>{heading}</h2><h3>Symptoms</h3>{table}"


def _condition_rows(fields: dict) -> list[dict]:
    raw = fields.get("conditions")
    if isinstance(raw, list):
        rows = [item for item in raw if isinstance(item, dict)]
        return [row for row in rows if _condition_present(row)]
    if _condition_present(fields):
        return [
            {
                "condition": fields.get("condition"),
                "threshold": fields.get("threshold"),
                "kind": fields.get("kind"),
                "namespace": fields.get("namespace"),
            }
        ]
    return []


def _condition_present(row: dict) -> bool:
    return any(str(row.get(name) or "").strip() for name in ("condition", "threshold", "kind", "namespace"))


def _condition_error(row: dict) -> str:
    condition = str(row.get("condition") or "").strip()
    threshold = str(row.get("threshold") or "").strip()
    kind = str(row.get("kind") or "").strip()
    namespace = str(row.get("namespace") or "").strip()
    if condition not in _WORKLOAD_CONDITIONS:
        return "Condition must be >, >=, or <."
    if not threshold:
        return "Threshold is required."
    if kind not in _WORKLOAD_KINDS:
        return "Type must be metric or log."
    if not namespace:
        return "Namespace or log group is required."
    return ""


def _storage_cell(name: str, value: str) -> str:
    escaped = html.escape(value)
    body = f"<code>{escaped}</code>" if name in _CODE_COLUMNS else escaped
    return f"<td><p>{body}</p></td>"


def _status_cell() -> str:
    macro = (
        '<ac:structured-macro ac:name="status" ac:schema-version="1">'
        '<ac:parameter ac:name="colour">Green</ac:parameter>'
        '<ac:parameter ac:name="title">Active</ac:parameter>'
        "</ac:structured-macro>"
    )
    return f"<td><p>{macro}</p></td>"


def _next_symptom_id(service: str, storage: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "", service).upper() or "SVC"
    text = storage or ""
    number = 1
    while True:
        candidate = f"{base}-S{number:02d}"
        if candidate not in text:
            return candidate
        number += 1


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode()
