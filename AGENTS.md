# Repository Guidelines

## Project Structure & Module Organization

This repository is currently design-first. `docs/MINIMAL.md` is authoritative;
application code and tests have not been added yet.

Planned layout:

- `app/`: CLI, storage, LLM stages, and export.
- `tests/`: user-visible workflow tests.
- `config/config.toml`: global configuration template.
- `prompts/`: global stage prompts.
- `projects/`: runtime project data; do not commit generated projects.

Avoid speculative abstraction layers. Let module boundaries follow actual code
size and responsibilities.

## Build, Test, and Development Commands

There is no build system, dependency file, or runnable package yet. Add only
commands that have been implemented and locally verified.

Planned CLI examples:

```bash
python -m app.main init INPUT --name demo
python -m app.main run-all demo
```

Once tests exist, use `pytest` and document setup in `README.md`.

## Coding Style & Naming Conventions

Use Python 3.11+, four-space indentation, public-function type hints, and UTF-8.
Use `snake_case` for functions/modules and `PascalCase` for classes.

Keep File, Segment, Run, and Chunk terminology consistent with `docs/MINIMAL.md`. Segment is the only progress unit; Chunk must never become persisted business state.

## Testing Guidelines

Name tests `test_<behavior>.py` and test observable behavior. Prioritize:

- TXT decoding and visual line structure.
- Segment-level recovery and dynamic Chunk rebuilding.
- Terminology, translation, proofreading, polishing, and apply workflows.
- Retry limits, validation repair, template sync, and bilingual export.

Use deterministic fixtures and mocked LLM responses. Quality percentages are
observational, not automated gates.

## Commit & Pull Request Guidelines

Keep `main` stable. Start every key stage from a clean `main` and create a focused
branch:

```bash
git switch main
git switch -c feat/segment-recovery
```

Use `feat/`, `fix/`, `docs/`, or `test/` plus a short kebab-case topic. Complete
each independently verifiable step with a Conventional Commit-style commit, such
as `feat: add segment recovery`. Do not combine unrelated steps or work directly
on `main`.

After the stage is complete, run the full applicable test and document checks.
Merge only with a clean worktree and successful validation:

```bash
git switch main
git merge --no-ff feat/segment-recovery
git branch -d feat/segment-recovery
```

Pull requests must explain behavior changes, affected specification sections,
completed steps, and validation. Call out new complexity and link relevant issues.

## Security & Configuration

Read API keys only from the configured environment variable. Never commit secrets, generated project data, raw debug payloads, or translated source material.
