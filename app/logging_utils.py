from __future__ import annotations

import logging
import sys
from pathlib import Path


LOGGER_NAME = "minimal_llm_translator"
_HANDLER_MARKER = "_minimal_llm_translator_handler"


class _StageFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "stage"):
            record.stage = "cli"
        return super().format(record)


def get_logger(stage: str = "cli") -> logging.LoggerAdapter:
    return logging.LoggerAdapter(logging.getLogger(LOGGER_NAME), {"stage": stage})


def _formatter() -> logging.Formatter:
    return _StageFormatter(
        fmt="%(asctime)s %(levelname)s [%(stage)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _remove_handler(kind: str) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, None) == kind:
            logger.removeHandler(handler)
            handler.close()


def configure_cli_logging() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _remove_handler("stderr")
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_MARKER, "stderr")
    handler.setLevel(logging.INFO)
    handler.setFormatter(_formatter())
    logger.addHandler(handler)


def attach_project_log(project: Path) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    _remove_handler("file")
    path = project / "logs" / "app.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    setattr(handler, _HANDLER_MARKER, "file")
    handler.setLevel(logging.INFO)
    handler.setFormatter(_formatter())
    logger.addHandler(handler)
