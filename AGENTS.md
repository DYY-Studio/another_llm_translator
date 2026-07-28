# Repository Guidelines

## Project Structure & Module Organization

This repository is currently design-first. The authoritative specification is
`docs/MINIMAL.md`; application code and tests have not been added yet.

When implementation begins, keep the layout small:

- `app/`: CLI, project storage, LLM execution, stages, and export logic.
- `tests/`: automated tests mirroring user-visible workflows.
- `config/config.toml`: global configuration template.
- `prompts/`: global terminology, translation, proofreading, and polishing prompts.
- `projects/`: runtime project data; do not commit generated projects.

Do not create abstraction layers or modules merely to match a speculative architecture. File and module boundaries should follow actual code size and responsibilities.

## Build, Test, and Development Commands

There is currently no build system, dependency file, or runnable package. Before adding commands here, define them in the repository and verify them locally.

The planned CLI shape is documented in `docs/MINIMAL.md`, for example:

```bash
python -m app.main init INPUT --name demo
python -m app.main run-all demo
python -m app.main export demo --stage translated
```

Once tests exist, use `pytest` as the default runner and document any required setup in `README.md`.

## Coding Style & Naming Conventions

Use Python 3.11+, four-space indentation, type hints for public functions, and UTF-8 source files. Prefer `snake_case` for functions and modules, `PascalCase` for classes, and stable uppercase-like values for persisted enums.

Keep File, Segment, Run, and Chunk terminology consistent with `docs/MINIMAL.md`. Segment is the only progress unit; Chunk must never become persisted business state.

## Testing Guidelines

Name tests `test_<behavior>.py` and test observable behavior rather than internal implementation. Prioritize:

- TXT decoding and visual line structure.
- Segment-level recovery and dynamic Chunk rebuilding.
- Terminology, translation, proofreading, polishing, and apply workflows.
- Retry limits, validation repair, template sync, and bilingual export.

Use deterministic fixtures and mocked LLM responses. Quality percentages are observational metrics, not hard automated gates.

## Commit & Pull Request Guidelines

No Git history is available in this workspace. Use concise Conventional Commit-style subjects, such as `docs: simplify MVP persistence` or `feat: add segment recovery`.

Pull requests should explain the user-visible change, identify affected specification sections, list verification performed, and call out any new complexity. Include screenshots only for future visual interfaces.

## Security & Configuration

Read API keys only from the configured environment variable. Never commit secrets, generated project data, raw debug payloads, or translated source material.
