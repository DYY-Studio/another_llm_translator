# Repository Guidelines

## Project Structure & Module Organization

- `app/`: CLI, project storage, execution, LLM stages, and TXT export.
- `tests/`: deterministic workflow tests using mocked LLM responses.
- `config/config.toml`: global configuration template.
- `prompts/`: global stage prompts.
- `projects/`: runtime project data; do not commit generated projects.
- `docs/ROADMAP.md`: authoritative MVP behavior and boundaries.

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
API key referenced by the Preset `credential` (an `environment` env var or a
`keychain` entry) in the environment or system keychain, never in TOML.

## Coding Guidelines

必须以完成当前明确需求为目标，优先选择最小、直接、易维护的实现。

* 仅实现已确认的需求，不为假设中的未来功能预留接口、配置、兼容路径或扩展框架。
* 优先修改现有代码；除非职责边界明确，不新增抽象层、包装类、工厂、注册中心或通用工具。
* 只有存在至少两个真实用例时才提取抽象。少量局部重复优于过早抽象。
* 不保留旧接口、旧参数或双执行路径，除非任务明确要求兼容真实调用方。仓库内调用方应同步更新。
* 外部输入在系统边界校验；内部可信调用不重复校验。
* 仅在能够恢复错误、释放资源或补充有效上下文时捕获异常。禁止无处理能力的捕获、重复日志和静默失败。
* 不添加 fallback、自动重试、feature flag 或降级逻辑，除非其属于明确的产品行为，并有可测试的触发条件。
* 关键决策必须由用户实际敲定，可警告和要求用户决策，不要尝试自动化决策流程。
* 必须保留鉴权、权限控制、数据保护、注入防护、事务一致性及其他与现实威胁模型对应的必要安全措施。
* 对不支持的配置、版本和运行环境应快速失败，不得猜测、自动修复或静默兼容。
* 不得顺便重构无关模块、统一风格或建设通用框架。
* 新增公共 API、配置项、依赖、文件、抽象层或兼容分支前，必须说明当前需求为何无法通过更小改动完成。
* 每项新增防御逻辑必须对应一个现实故障场景、实际调用路径和测试。
* 完成实现后必须进行减法审查，删除未使用代码、推测性设计、重复校验、无效包装和不必要分支。
* 在功能、测试和必要安全属性均满足时，代码量更少、状态更少、分支更少、依赖更少的方案优先。

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

Keep `dev` stable. Start each key stage on a focused branch:

```bash
git switch dev
git switch -c feat/segment-recovery
```

Commit each key step you have made.
When creating git commits:
- Keep current git user identity.
- Add: Co-authored-by: Codex <codex@openai.com>

Use `feat/`, `fix/`, `docs/`, or `test/` and Conventional Commit messages such as
`feat: add segment recovery`. Commit each independently verified step. After the
stage passes its tests and `git diff --check`, merge with an explicit merge commit:

```bash
git switch dev
git merge --no-ff feat/segment-recovery
git branch -d feat/segment-recovery
```

Pull requests must describe behavior, affected specification sections, and exact
validation commands. Call out added complexity and link relevant issues.

## Security & Configuration

Never commit secrets, generated projects, debug payloads, or source material.
