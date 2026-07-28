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


class IncompleteError(AppError):
    exit_code = 5

