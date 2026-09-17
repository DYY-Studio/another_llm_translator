from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.errors import ConfigError
from app.plugin_api import PLUGIN_PROTOCOL_VERSION
from app.plugins import (
    _PLUGIN_NAMESPACE_PREFIX,
    _official_plugin_root,
    load_plugins,
)


def _write_plugin(
    root: Path,
    directory: str,
    *,
    plugin_id: str,
    body: str,
    manifest_id: str | None = None,
    version: str = "1.0.0",
    protocol: int = PLUGIN_PROTOCOL_VERSION,
    entrypoint: str = "plugin:descriptor",
) -> Path:
    plugin = root / directory
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text(
        "schema = 1\n\n"
        "[plugin]\n"
        f'id = "{manifest_id or plugin_id}"\n'
        f'version = "{version}"\n'
        f"protocol = {protocol}\n"
        f'entrypoint = "{entrypoint}"\n',
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "plugin.py").write_text(body, encoding="utf-8")
    return plugin


def _validator_body(
    descriptor_id: str,
    marker: Path | None = None,
    count_marker: Path | None = None,
) -> str:
    marker_code = (
        f"    Path({str(marker)!r}).write_text('loaded', encoding='utf-8')\n"
        if marker is not None
        else ""
    )
    count_code = (
        "    current = int(Path({path!r}).read_text() or '0') if Path({path!r}).exists() else 0\n"
        "    Path({path!r}).write_text(str(current + 1), encoding='utf-8')\n"
        .format(path=str(count_marker))
        if count_marker is not None
        else ""
    )
    return (
        "from pathlib import Path\n"
        "from app.plugin_api import PluginDescriptor\n"
        "\n"
        "class Validator:\n"
        "    validator_id = 'fixture_' + " + repr(descriptor_id) + "\n"
        "    version = '1'\n"
        "    label = 'Fixture'\n"
        "    def validate(self, context):\n"
        "        return ()\n"
        "\n"
        "def descriptor():\n"
        f"{marker_code}"
        f"{count_code}"
        f"    return PluginDescriptor({descriptor_id!r}, '1.0.0', 12, translation_validators=(Validator(),))\n"
    )


def test_directory_plugins_are_sorted_and_loaded_from_user_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = tmp_path / "official"
    user = tmp_path / "user"
    user_plugins = user / "plugins"
    _write_plugin(official, "z-official", plugin_id="z-official", body=_validator_body("z-official"))
    _write_plugin(official, "a-official", plugin_id="a-official", body=_validator_body("a-official"))
    _write_plugin(user_plugins, "user-plugin", plugin_id="user-plugin", body=_validator_body("user-plugin"))
    monkeypatch.setattr("app.plugins._official_plugin_root", lambda: official)
    monkeypatch.setattr("app.plugins.user_root", lambda: user)
    monkeypatch.setattr("app.plugins._PLUGIN_CACHE", None)

    descriptors = load_plugins()

    assert [item.plugin_id for item in descriptors[-3:]] == [
        "a-official",
        "z-official",
        "user-plugin",
    ]


def test_official_plugin_root_uses_builtin_prefix_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builtin_root = tmp_path / "prefix"
    monkeypatch.setattr("app.plugins.BUILTIN_ROOT", builtin_root)

    assert _official_plugin_root() == builtin_root / "plugins"


def test_user_plugin_root_file_is_a_discovery_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = tmp_path / "official"
    _write_plugin(
        official,
        "official",
        plugin_id="official",
        body=_validator_body("official"),
    )
    user = tmp_path / "user"
    user.mkdir()
    (user / "plugins").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr("app.plugins._official_plugin_root", lambda: official)
    monkeypatch.setattr("app.plugins.user_root", lambda: user)
    monkeypatch.setattr("app.plugins._PLUGIN_CACHE", None)

    with pytest.raises(ConfigError, match="discover") as raised:
        load_plugins()

    assert str(user / "plugins") in str(raised.value)


def test_manifest_errors_are_checked_before_any_plugin_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = tmp_path / "official"
    marker = tmp_path / "imported"
    _write_plugin(
        official,
        "good",
        plugin_id="good",
        body=_validator_body("good", marker),
    )
    (official / "broken").mkdir(parents=True)
    monkeypatch.setattr("app.plugins._official_plugin_root", lambda: official)
    monkeypatch.setattr("app.plugins.user_root", lambda: tmp_path / "missing-user")
    monkeypatch.setattr("app.plugins._PLUGIN_CACHE", None)
    with pytest.raises(ConfigError, match="缺少 plugin.toml"):
        load_plugins()
    assert not marker.exists()


def test_user_plugin_id_collision_with_builtin_fails_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = tmp_path / "official"
    user = tmp_path / "user"
    user_plugins = user / "plugins"
    _write_plugin(official, "official", plugin_id="official", body=_validator_body("official"))
    marker = tmp_path / "imported"
    _write_plugin(
        user_plugins,
        "collision",
        plugin_id="collision",
        manifest_id="builtin-documents",
        body=_validator_body("collision", marker),
    )
    monkeypatch.setattr("app.plugins._official_plugin_root", lambda: official)
    monkeypatch.setattr("app.plugins.user_root", lambda: user)
    monkeypatch.setattr("app.plugins._PLUGIN_CACHE", None)

    with pytest.raises(ConfigError, match="占用内置 ID") as raised:
        load_plugins()
    assert "内置插件：builtin-documents" in str(raised.value)
    assert str(user_plugins / "collision") in str(raised.value)
    assert not marker.exists()


def test_manifest_and_descriptor_identity_must_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = tmp_path / "official"
    _write_plugin(
        official,
        "mismatch",
        plugin_id="manifest-id",
        body=_validator_body("descriptor-id"),
    )
    monkeypatch.setattr("app.plugins._official_plugin_root", lambda: official)
    monkeypatch.setattr("app.plugins.user_root", lambda: tmp_path / "missing-user")
    monkeypatch.setattr("app.plugins._PLUGIN_CACHE", None)

    with pytest.raises(ConfigError, match="manifest 与 PluginDescriptor"):
        load_plugins()


def test_successful_directory_load_is_cached_across_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = tmp_path / "official"
    marker = tmp_path / "factory-count"
    _write_plugin(
        official,
        "cached",
        plugin_id="cached",
        body=_validator_body("cached", count_marker=marker),
    )
    monkeypatch.setattr("app.plugins._official_plugin_root", lambda: official)
    monkeypatch.setattr("app.plugins.user_root", lambda: tmp_path / "missing-user")
    monkeypatch.setattr("app.plugins._PLUGIN_CACHE", None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: load_plugins(), range(4)))

    assert all(result is results[0] for result in results)
    assert marker.read_text(encoding="utf-8") == "1"


def test_failed_load_cleans_imported_namespaces_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = tmp_path / "official"
    marker = tmp_path / "module-import-count"
    good_body = (
        "from pathlib import Path\n"
        f"marker = Path({str(marker)!r})\n"
        "count = int(marker.read_text() or '0') if marker.exists() else 0\n"
        "marker.write_text(str(count + 1), encoding='utf-8')\n"
        + _validator_body("good")
    )
    _write_plugin(official, "a-good", plugin_id="good", body=good_body)
    broken = _write_plugin(
        official,
        "z-broken",
        plugin_id="broken",
        body="raise RuntimeError('broken plugin')\n",
    )
    monkeypatch.setattr("app.plugins._official_plugin_root", lambda: official)
    monkeypatch.setattr("app.plugins.user_root", lambda: tmp_path / "missing-user")
    monkeypatch.setattr("app.plugins._PLUGIN_CACHE", None)
    namespaces_before = {
        name for name in sys.modules if name.startswith(_PLUGIN_NAMESPACE_PREFIX)
    }

    with pytest.raises(ConfigError, match="z-broken.*broken plugin"):
        load_plugins()
    assert marker.read_text(encoding="utf-8") == "1"
    namespaces_after = {
        name for name in sys.modules if name.startswith(_PLUGIN_NAMESPACE_PREFIX)
    }
    assert namespaces_after == namespaces_before

    broken.joinpath("plugin.py").write_text(
        _validator_body("broken"), encoding="utf-8"
    )
    descriptors = load_plugins()

    assert {item.plugin_id for item in descriptors[-2:]} == {"good", "broken"}
    assert marker.read_text(encoding="utf-8") == "2"
