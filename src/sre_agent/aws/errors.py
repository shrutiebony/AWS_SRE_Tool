"""Errors raised by cloud readers. Callers must not treat denial as absence."""


class CloudError(Exception):
    """Base class for expected cloud-reader failures."""


class AccessDenied(CloudError):
    """The caller is not allowed to read or change this resource."""


class NotFound(CloudError):
    """A direct lookup succeeded and the resource is not there."""


class Throttled(CloudError):
    """The API asked the caller to slow down."""


class SourceNotConfigured(CloudError):
    """An optional inventory source is not turned on in this scope."""


class PartialRead(CloudError):
    """A later page failed. items holds what was read before the failure."""

    def __init__(self, message: str, items: list | None = None) -> None:
        super().__init__(message)
        self.items = items or []


_THROTTLE_CODES = {
    "Throttling",
    "ThrottlingException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
    "ProvisionedThroughputExceededException",
    "SlowDown",
}

_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AuthorizationError",
    "AccessDeniedException",
    "OptInRequired",
}

_NOT_FOUND_CODES = {
    "ResourceNotFoundException",
    "NoSuchEntity",
    "NotFound",
    "NoSuchBucket",
    "InvalidInstanceID.NotFound",
    "NonExistentQueue",
    "AWS.SimpleQueueService.NonExistentQueue",
    "QueueDoesNotExist",
    "FunctionError",
}


def classify_error_code(code: str) -> str:
    """Map an AWS error code to denied, throttled, not_found, or other."""
    if code in _THROTTLE_CODES:
        return "throttled"
    if code in _DENIED_CODES:
        return "denied"
    if code in _NOT_FOUND_CODES:
        return "not_found"
    return "other"


def map_exception(exc: Exception) -> CloudError | None:
    """Convert a boto-style ClientError into a reader error, when recognized."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    code = str(response.get("Error", {}).get("Code", ""))
    kind = classify_error_code(code)
    message = str(response.get("Error", {}).get("Message", code))
    if kind == "throttled":
        return Throttled(message or code)
    if kind == "denied":
        return AccessDenied(message or code)
    if kind == "not_found":
        return NotFound(message or code)
    return None
