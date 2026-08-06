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
hiddenimports = collect_submodules("uvicorn") + collect_submodules("fastapi")

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
    ],
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
