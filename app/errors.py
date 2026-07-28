class AppError(Exception):
    """Base class for expected command failures."""

    exit_code = 1


class UsageError(AppError):
    exit_code = 2


class ConfigError(AppError):
    exit_code = 2


class ProjectError(AppError):
    exit_code = 3


class StorageError(AppError):
    exit_code = 3


class ExternalError(AppError):
    exit_code = 4


class ContextLengthError(ExternalError):
    """The remote model rejected a request as too large."""

    def __init__(self, message: str, *, request_id: str) -> None:
        super().__init__(message)
        self.request_id = request_id


class FatalExternalError(ExternalError):
    """Authentication or endpoint failure that stops the whole stage."""


class IncompleteError(AppError):
    exit_code = 5
