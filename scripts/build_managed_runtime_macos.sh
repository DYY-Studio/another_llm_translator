#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: build_managed_runtime_macos.sh \
  --runtime-source PBS_INSTALL_DIR \
  --pbs-lock PBS_LOCK_JSON \
  --pbs-archive PBS_ARCHIVE \
  --project-wheel PROJECT.whl \
  --wheelhouse WHEELHOUSE_DIR \
  --requirements-lock REQUIREMENTS.lock \
  --output DESTINATION_DIR
EOF
}

fail() {
    printf 'build_managed_runtime_macos.sh: %s\n' "$*" >&2
    exit 1
}

runtime_source=
pbs_lock=
pbs_archive=
project_wheel=
wheelhouse=
requirements_lock=
output=
while (($#)); do
    if (($# < 2)); then
        usage >&2
        fail "missing value for $1"
    fi
    case "$1" in
        --runtime-source) runtime_source=$2 ;;
        --pbs-lock) pbs_lock=$2 ;;
        --pbs-archive) pbs_archive=$2 ;;
        --project-wheel) project_wheel=$2 ;;
        --wheelhouse) wheelhouse=$2 ;;
        --requirements-lock) requirements_lock=$2 ;;
        --output) output=$2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "unknown option $1" ;;
    esac
    shift 2
done

for name in runtime_source pbs_lock pbs_archive project_wheel wheelhouse requirements_lock output; do
    if [[ -z ${!name} ]]; then
        fail "--${name//_/-} is required"
    fi
done

[[ -d $runtime_source ]] || fail "--runtime-source is not a directory: $runtime_source"
[[ -x $runtime_source/bin/python3 ]] || fail "--runtime-source lacks executable bin/python3: $runtime_source"
[[ -f $pbs_lock ]] || fail "--pbs-lock does not exist: $pbs_lock"
[[ -f $pbs_archive ]] || fail "PBS archive SHA-256 mismatch: archive missing: $pbs_archive"
[[ -f $project_wheel && $project_wheel == *.whl ]] || fail "--project-wheel must be an existing .whl file: $project_wheel"
[[ -d $wheelhouse ]] || fail "--wheelhouse is not a directory: $wheelhouse"
[[ -f $requirements_lock ]] || fail "--requirements-lock does not exist: $requirements_lock"
[[ $output == build/managed-runtime-dist ]] || fail "--output must be build/managed-runtime-dist"
[[ ! -L $output ]] || fail "--output must not be a symbolic link: $output"
[[ ! -L build ]] || fail "build output parent must not be a symbolic link"

runtime_source=$(cd "$runtime_source" && pwd -P)
pbs_lock=$(cd "$(dirname "$pbs_lock")" && pwd -P)/$(basename "$pbs_lock")
pbs_archive=$(cd "$(dirname "$pbs_archive")" && pwd -P)/$(basename "$pbs_archive")
project_wheel=$(cd "$(dirname "$project_wheel")" && pwd -P)/$(basename "$project_wheel")
wheelhouse=$(cd "$wheelhouse" && pwd -P)
requirements_lock=$(cd "$(dirname "$requirements_lock")" && pwd -P)/$(basename "$requirements_lock")
mkdir -p build
output_parent=$(cd build && pwd -P)
output=$output_parent/managed-runtime-dist
for input in "$runtime_source" "$wheelhouse"; do
    [[ $output != "$input" && $output != "$input/"* && $input != "$output/"* ]] \
        || fail "--output must be separate from input directory: $input"
done
for input in "$pbs_lock" "$project_wheel" "$requirements_lock"; do
    [[ $output != "$input" ]] || fail "--output must be separate from input file: $input"
done

python=$runtime_source/bin/python3
runtime_info=$(
    "$python" -I -c '
import json
import hashlib
import platform
import sys
import sysconfig

from pathlib import Path

lock_path = Path(sys.argv[1])
archive_path = Path(sys.argv[2])
try:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    output_path = Path(sys.argv[3]).resolve()
    for input_path in map(Path, sys.argv[4:]):
        input_path = input_path.resolve()
        if input_path == output_path or output_path in input_path.parents:
            raise SystemExit(f"input is inside output: {input_path}")
    archive_name = lock.get("asset_filename")
    archive_hash = lock.get("sha256")
    with archive_path.open("rb") as archive:
        actual_hash = hashlib.file_digest(archive, "sha256").hexdigest()
    if archive_name != archive_path.name or archive_hash != actual_hash:
        raise SystemExit(
            f"PBS archive SHA-256 mismatch: expected {archive_hash!r} for "
            f"{archive_name!r}, got {actual_hash} for {archive_path.name!r}"
        )
    version = sys.version.split()[0]
    target = sysconfig.get_config_var("HOST_GNU_TYPE")
    gil_disabled = sysconfig.get_config_var("Py_GIL_DISABLED")
except (OSError, json.JSONDecodeError, TypeError) as exc:
    raise SystemExit(f"PBS lock/runtime check failed for {lock_path}: {exc}")
if version != lock.get("python_version"):
    raise SystemExit(
        f"PBS runtime {version} does not match lock version {lock.get('python_version')!r}"
    )
if target != lock.get("target") or platform.machine() != "arm64":
    raise SystemExit(
        f"PBS runtime target mismatch: lock={lock.get('target')!r}, "
        f"runtime={target!r}, machine={platform.machine()!r}"
    )
if lock.get("gil_enabled") is not True or gil_disabled not in (0, "0"):
    raise SystemExit("PBS runtime must be GIL-enabled")
print(f"{version} {target} arm64 GIL-enabled")
' "$pbs_lock" "$pbs_archive" "$output" \
    "$pbs_lock" "$pbs_archive" "$project_wheel" "$requirements_lock"
) || fail "PBS runtime or input validation failed for $pbs_lock"
printf 'Using verified PBS runtime: %s\n' "$runtime_info"

build_root=$(mktemp -d "$output_parent/.managed-runtime-build.XXXXXX")
trap 'rm -rf "$build_root"' EXIT
runtime=$build_root/managed-runtime
mkdir "$runtime"
cp -R "$runtime_source"/. "$runtime"/
mkdir -p "$runtime/share/managed-runtime"
cp "$pbs_lock" "$runtime/share/managed-runtime/pbs-macos-arm64.lock.json"
original_bin=$build_root/original-bin
mkdir "$original_bin"
for item in "$runtime/bin"/*; do
    [[ -e $item || -L $item ]] || continue
    : > "$original_bin/$(basename "$item")"
done

run_pip() {
    env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV \
        "$runtime/bin/python3" -I -m pip \
        --disable-pip-version-check --no-input --no-cache-dir "$@"
}

run_pip install --no-index --prefix "$runtime" --no-deps --no-compile "$project_wheel"
run_pip install --no-index --prefix "$runtime" --no-deps --only-binary=:all: --no-compile \
    --find-links "$wheelhouse" --require-hashes -r "$requirements_lock"

# The app runs `python -m app.web`; pip-created console scripts embed the temporary staging path.
for item in "$runtime/bin"/*; do
    [[ -e $item || -L $item ]] || continue
    if [[ ! -e $original_bin/$(basename "$item") ]]; then
        rm -rf "$item"
    fi
done

python_minor=${runtime_info%% *}
python_minor=${python_minor%.*}
site_packages=$runtime/lib/python$python_minor/site-packages
rm -f "$runtime/bin/pip" "$runtime/bin/pip3" "$runtime/bin/pip$python_minor"
rm -rf "$runtime/lib/python$python_minor/ensurepip"
for path in "$site_packages/pip" "$site_packages"/pip-*.dist-info; do
    [[ ! -e $path ]] || rm -rf "$path"
done

[[ -x $runtime/bin/python3 ]] || fail "staging lost bin/python3"
for resource in \
    "$runtime/config/config.toml" \
    "$runtime/prompts/translation.en.middle.txt" \
    "$runtime/llm_adapters/openai-compatible.json" \
    "$runtime/llm_presets/default.json" \
    "$runtime/plugins/srt/plugin.toml" \
    "$runtime/plugins/srt/__init__.py" \
    "$runtime/plugins/srt/adapter.py" \
    "$runtime/plugins/srt/plugin.py" \
    "$runtime/plugins/term_validation/plugin.toml" \
    "$runtime/plugins/term_validation/plugin.py"; do
    [[ -f $resource ]] || fail "project wheel did not install official plugin resource: $resource"
done

previous=
if [[ -e $output ]]; then
    [[ -d $output ]] || fail "--output exists and is not a directory: $output"
    previous=$build_root/previous-output
    mv "$output" "$previous"
fi
if ! mv "$runtime" "$output"; then
    [[ -z $previous ]] || mv "$previous" "$output"
    fail "could not move managed runtime into $output"
fi
printf 'Managed runtime staged at %s\n' "$output"
