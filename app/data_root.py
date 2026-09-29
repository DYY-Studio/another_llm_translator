from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from .user_config import (
    USER_ROOT_NAME,
    USER_ROOT_OVERRIDE_ENV,
    _located_user_root,
    default_user_root,
    user_root,
    user_root_locator_path,
)

PENDING_NAME = f"{USER_ROOT_NAME}-relocation.json"
MARKER_NAME = ".another-llm-relocation.json"


def pending_path() -> Path:
    return user_root_locator_path().with_name(PENDING_NAME)


def transaction_marker(target: Path) -> Path:
    return target / MARKER_NAME


def staging_marker_path(target: Path, transaction_id: str) -> Path:
    staging = target.with_name(f".{target.name}.staging-{transaction_id}")
    return staging.with_name(f"{staging.name}.json")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {description} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(  # noqa: TRY004 - CLI treats malformed files as user errors.
            f"invalid {description} {path}: expected an object"
        )
    return value


def _active_root_without_environment() -> Path:
    return _located_user_root() or default_user_root()


def _reject_environment_override() -> None:
    if os.environ.get(USER_ROOT_OVERRIDE_ENV):
        raise ValueError(
            f"environment variable {USER_ROOT_OVERRIDE_ENV} controls the active user root"
        )


def _validated_paths(source: Path, target_parent: Path) -> tuple[Path, Path]:
    source = source.expanduser().absolute()
    target_parent = target_parent.expanduser().absolute()
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"source user root is unavailable: {source}")
    if not target_parent.is_dir() or not os.access(target_parent, os.W_OK | os.X_OK):
        raise ValueError(f"target parent is unavailable: {target_parent}")
    source = source.resolve()
    target = target_parent.resolve() / USER_ROOT_NAME
    if (
        source == target
        or source.is_relative_to(target)
        or target.is_relative_to(source)
    ):
        raise ValueError("source and target roots must not overlap")
    return source, target


def write_pending(source: Path, target: Path, transaction_id: str) -> None:
    _atomic_json(
        pending_path(),
        {
            "version": 1,
            "transaction_id": transaction_id,
            "source_root": str(source),
            "target_root": str(target),
        },
    )


def request_relocation(parent_dir: Path) -> dict[str, str]:
    _reject_environment_override()
    source, target = _validated_paths(_active_root_without_environment(), parent_dir)
    if target.exists() or target.is_symlink():
        raise ValueError(f"target user root already exists: {target}")
    transaction_id = uuid.uuid4().hex
    write_pending(source, target, transaction_id)
    return {"source_root": str(source), "target_root": str(target)}


def status() -> dict[str, Any]:
    override = os.environ.get(USER_ROOT_OVERRIDE_ENV)
    active = user_root()
    if override:
        mode = "environment"
    elif active == default_user_root():
        mode = "default"
    else:
        mode = "custom"
    pending = pending_path()
    return {
        "active_root": str(active),
        "default_root": str(default_user_root()),
        "mode": mode,
        "pending": _read_json(pending, "relocation request")
        if pending.exists()
        else None,
    }


def cancel_relocation(*, confirm: bool) -> dict[str, bool]:
    _reject_environment_override()
    if not confirm:
        raise ValueError("cancelling relocation requires explicit confirmation")
    pending = pending_path()
    cancelled = pending.exists() or pending.is_symlink()
    pending.unlink(missing_ok=True)
    return {"cancelled": cancelled}


def _read_pending() -> tuple[Path, Path, str]:
    value = _read_json(pending_path(), "relocation request")
    try:
        source = Path(value["source_root"])
        target = Path(value["target_root"])
        transaction_id = value["transaction_id"]
        if (
            value.get("version") != 1
            or not source.is_absolute()
            or not target.is_absolute()
            or source.name != USER_ROOT_NAME
            or target.name != USER_ROOT_NAME
            or not isinstance(transaction_id, str)
            or not transaction_id
        ):
            raise ValueError("invalid fields")
        if (
            source == target
            or source.is_relative_to(target)
            or target.is_relative_to(source)
        ):
            raise ValueError("source and target roots must not overlap")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid relocation request {pending_path()}: {exc}") from exc
    return source, target, transaction_id


def _has_our_marker(target: Path, source: Path, transaction_id: str) -> bool:
    return _has_transaction_marker(
        transaction_marker(target), source, target, transaction_id
    )


def _has_transaction_marker(
    marker: Path, source: Path, target: Path, transaction_id: str
) -> bool:
    if not marker.is_file():
        return False
    value = _read_json(marker, "relocation transaction marker")
    return value == {
        "transaction_id": transaction_id,
        "source_root": str(source),
        "target_root": str(target),
    }


def _tree_manifest(root: Path) -> dict[str, tuple[str, int | str | None]]:
    manifest: dict[str, tuple[str, int | str | None]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entry = ("symlink", os.readlink(path))
        elif path.is_dir():
            entry = ("directory", None)
        elif path.is_file():
            entry = ("file", path.stat().st_size)
        else:
            entry = ("other", None)
        manifest[relative] = entry
    return manifest


def _write_locator(target: Path) -> None:
    _atomic_json(user_root_locator_path(), {"version": 1, "active_root": str(target)})


def apply_pending() -> dict[str, str]:
    _reject_environment_override()
    source, target, transaction_id = _read_pending()
    canonical_source = source.resolve()
    canonical_parent = target.parent.resolve()
    if canonical_source != source or canonical_parent != target.parent:
        raise ValueError("source or target parent canonical path changed")
    target = canonical_parent / USER_ROOT_NAME
    if (
        canonical_source == target
        or canonical_source.is_relative_to(target)
        or target.is_relative_to(canonical_source)
    ):
        raise ValueError("source and target roots must not overlap")
    source = canonical_source
    current = _active_root_without_environment().resolve()
    if current not in (source.resolve(), target.resolve()):
        raise ValueError("active user root no longer matches the relocation request")

    marker = transaction_marker(target)
    has_marker = False
    if target.is_symlink():
        raise ValueError(f"target user root is a symbolic link: {target}")
    if target.exists():
        has_marker = _has_our_marker(target, source, transaction_id)
        completed_cleanup = current == target.resolve() and not source.exists()
        if not has_marker and not completed_cleanup:
            raise ValueError(f"target user root already exists: {target}")
    else:
        if current != source.resolve():
            raise ValueError("published target for this relocation is missing")
        if not source.is_dir() or source.is_symlink():
            raise ValueError(f"source user root is unavailable: {source}")
        if not target.parent.is_dir() or not os.access(
            target.parent, os.W_OK | os.X_OK
        ):
            raise ValueError(f"target parent is unavailable: {target.parent}")
        staging = target.with_name(f".{target.name}.staging-{transaction_id}")
        staging_marker = staging_marker_path(target, transaction_id)
        if staging_marker.exists():
            if not _has_transaction_marker(
                staging_marker, source, target, transaction_id
            ):
                raise ValueError(
                    f"staging marker does not match this transaction: {staging_marker}"
                )
            if staging.exists():
                shutil.rmtree(staging)
            staging_marker.unlink()
        elif staging.exists():
            raise ValueError(f"staging directory has no transaction marker: {staging}")
        created_staging = False
        try:
            _atomic_json(
                staging_marker,
                {
                    "transaction_id": transaction_id,
                    "source_root": str(source),
                    "target_root": str(target),
                },
            )
            staging.mkdir()
            created_staging = True
            shutil.copytree(source, staging, symlinks=True, dirs_exist_ok=True)
            if _tree_manifest(source) != _tree_manifest(staging):
                raise ValueError("data root copy verification failed")
            _atomic_json(
                transaction_marker(staging),
                {
                    "transaction_id": transaction_id,
                    "source_root": str(source),
                    "target_root": str(target),
                },
            )
            if target.exists() or target.is_symlink():
                raise ValueError(f"target user root already exists: {target}")
            os.replace(staging, target)
            has_marker = True
        except BaseException:
            if created_staging and staging.exists():
                try:
                    shutil.rmtree(staging)
                except OSError:
                    pass
            if not created_staging or not staging.exists():
                staging_marker.unlink(missing_ok=True)
            raise

    staging_marker = staging_marker_path(target, transaction_id)
    if staging_marker.exists():
        if not _has_transaction_marker(staging_marker, source, target, transaction_id):
            raise ValueError(
                f"staging marker does not match this transaction: {staging_marker}"
            )
        staging_marker.unlink()

    if current != target.resolve():
        _write_locator(target)
    if source.exists():
        try:
            shutil.rmtree(source)
        except OSError as exc:
            return {
                "active_root": str(target),
                "old_root": str(source),
                "warning": (
                    "New data root is active; old root was not removed: "
                    f"{source} ({exc})"
                ),
            }
    if has_marker and marker.exists():
        marker.unlink()
    pending_path().unlink(missing_ok=True)
    return {"active_root": str(target), "removed_source_root": str(source)}


def reset_to_default(*, confirm: bool) -> dict[str, str]:
    _reject_environment_override()
    if not confirm:
        raise ValueError("reset requires explicit confirmation")
    pending_path().unlink(missing_ok=True)
    locator = user_root_locator_path()
    if locator.exists():
        locator.unlink()
    return {"active_root": str(default_user_root())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.data_root")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show the active user data root")
    commands.add_parser("apply-pending", help="apply a previously requested relocation")
    reset = commands.add_parser("reset", help="use the platform default root")
    reset.add_argument("--confirm", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            result = status()
        elif args.command == "apply-pending":
            result = apply_pending()
        else:
            result = reset_to_default(confirm=args.confirm)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
