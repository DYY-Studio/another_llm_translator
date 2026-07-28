# Repository Guidelines

## Project Structure & Module Organization

- `app/`: CLI, project storage, execution, LLM stages, and TXT export.
- `tests/`: deterministic workflow tests using mocked LLM responses.
- `config/config.toml`: global configuration template.
- `prompts/`: global stage prompts.
- `projects/`: runtime project data; do not commit generated projects.
- `docs/MINIMAL.md`: authoritative MVP behavior and boundaries.

Preserve the File/Segment/Chunk/Run meanings in the specification. Segment is
the progress unit; Chunk is never durable business state. Avoid speculative
providers, plugins, databases, and generic service layers.

## Build, Test, and Development Commands

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip check
python -m pytest -q
python -m app.main --help
```

Use `python -m app.main init INPUT --name PROJECT` to create a project. Put the
API key named by `llm.api_key_env` in the environment, never in TOML.

## Coding Style & Naming Conventions

Use Python 3.11+, four-space indentation, public-function type hints, and UTF-8.
Use `snake_case` for functions/modules and `PascalCase` for classes. Prefer
standard-library features and small functions over new abstraction layers.

## Testing Guidelines

Name tests `test_<behavior>.py`. Test observable behavior with temporary projects
and `httpx.MockTransport`; never call a live model. Run the full suite before
merging. Quality percentages in the specification are observational, not test
gates.

## Commit & Pull Request Guidelines

Keep `main` stable. Start each key stage on a focused branch:

```bash
git switch main
git switch -c feat/segment-recovery
```

Use `feat/`, `fix/`, `docs/`, or `test/` and Conventional Commit messages such as
`feat: add segment recovery`. Commit each independently verified step. After the
stage passes its tests and `git diff --check`, merge with an explicit merge commit:

```bash
git switch main
git merge --no-ff feat/segment-recovery
git branch -d feat/segment-recovery
```

Pull requests must describe behavior, affected specification sections, and exact
validation commands. Call out added complexity and link relevant issues.

## Security & Configuration

Never commit secrets, generated projects, debug payloads, or source material.
