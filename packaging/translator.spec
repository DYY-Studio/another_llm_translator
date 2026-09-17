# PyInstaller spec for the Python/FastAPI sidecar.
#
# Built by scripts/build-sidecar.sh; output lands in sidecar-dist/
# (gitignored). The Tauri bundle ships that directory as a resource and
# the Rust shell execs sidecar-dist/translator-sidecar when present,
# falling back to `python -m app.web` in development.
#
# Bundled app resources keep the source layout so
# user_config.BUILTIN_ROOT / config.APP_ROOT resolve to sys.prefix
# (_MEIPASS) inside the frozen app.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH) if "SPECPATH" in globals() else Path.cwd()


def collect_plugin_datas(plugin_root: Path) -> list[tuple[str, str]]:
    """Bundle only runtime plugin files and their manifests."""
    datas: list[tuple[str, str]] = []
    for path in sorted(plugin_root.rglob("*")):
        relative = path.relative_to(plugin_root)
        if (
            not path.is_file()
            or "tests" in relative.parts
            or "__pycache__" in relative.parts
            or (path.suffix != ".py" and path.name != "plugin.toml")
        ):
            continue
        destination = Path("plugins") / relative.parent
        datas.append((str(path), str(destination)))
    return datas


plugin_root = ROOT.parent / "plugins"
plugin_datas = collect_plugin_datas(plugin_root)
hiddenimports = collect_submodules("uvicorn") + [
    # These modules are reached through directory-loaded plugin code or
    # imports inside the builtin plugin factory, so static analysis cannot
    # discover all of them from sidecar_entry.py.
    "app.documents",
    "app.epub_adapter",
    "app.errors",
    "app.plugin_api",
    "app.plugins",
    "app.project",
    "app.translation_validation",
]

a = Analysis(
    [str(ROOT / "sidecar_entry.py")],
    pathex=[str(ROOT.parent)],
    binaries=[],
    datas=[
        (str(ROOT.parent / "config"), "config"),
        (str(ROOT.parent / "prompts"), "prompts"),
        (str(ROOT.parent / "llm_adapters"), "llm_adapters"),
        (str(ROOT.parent / "llm_presets"), "llm_presets"),
        (str(ROOT.parent / "app" / "web_dist"), "app/web_dist"),
    ] + plugin_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="translator-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="translator-sidecar",
)
