class AppError(Exception):
    """Base class for expected command failures."""

    exit_code = 1
    code = "app_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.params: dict[str, object] = {}


class UsageError(AppError):
    exit_code = 2
    code = "usage_error"


class ConfigError(AppError):
    exit_code = 2
    code = "config_error"


class RequestSizeError(ConfigError):
    """A locally estimated request exceeds a configured input limit."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.params = {"reason": reason}


class ProjectError(AppError):
    exit_code = 3
    code = "project_error"


class StorageError(AppError):
    exit_code = 3
    code = "storage_error"


class ExternalError(AppError):
    exit_code = 4
    code = "external_error"


class ContextLengthError(ExternalError):
    """The remote model rejected a request as too large."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        segment_ids: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.segment_ids = segment_ids


class FatalExternalError(ExternalError):
    """Authentication or endpoint failure that stops the whole stage."""


class IncompleteError(AppError):
    exit_code = 5
    code = "incomplete_error"
