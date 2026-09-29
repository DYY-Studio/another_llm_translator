# Repository Guidelines

## 1. 工作原则

以完成当前明确需求为目标，优先采用最小、直接、可验证的实现。

* 只实现已确认需求，不为假设中的未来功能预留框架、兼容路径或扩展点。
* 优先修改现有实现；只有职责边界明确且存在真实复用需求时才新增抽象。
* 不保留旧接口、双执行路径、fallback、feature flag 或自动降级，除非明确存在兼容需求或产品规则。
* 外部输入在系统边界校验；内部可信路径避免重复校验。
* 仅在能够恢复错误、释放资源或补充有效上下文时捕获异常。
* 用户决策不得被程序静默替代。设置变化、结果复用、术语冲突、建议应用等应保持显式。
* 保留鉴权、数据保护、注入防护、写锁、事务一致性及其他真实安全边界。
* 不顺便重构无关代码，不因单一用例建设通用框架。
* 完成后进行减法审查，删除未使用代码、重复/无效校验、无效包装和不必要分支。
* 编写测试前必须先思考该测试是否真的有意义。

在功能、测试和必要安全属性相同的前提下，优先状态更少、分支更少、依赖更少的方案。

## 2. 核心领域边界

必须保持以下概念职责稳定：

* **File**：源文档、选择和导出的边界。
* **Segment**：持久化内容及进度恢复单位。
* **Chunk**：一次模型请求的临时分组，不是持久业务状态。
* **Run**：一次阶段执行记录。

不得使用 Chunk 推断长期进度，也不得混淆 File、Segment、Chunk 和 Run。

完整产品行为以 `docs/MINIMAL.md` 为准；Adapter 与插件协议以 `docs/ADAPTERS.md` 为准。

## 3. 模块职责

后端按业务职责组织，避免把已拆分职责重新集中到大型模块。

* `app/execution.py`：Scope、Chunk、Run、调度和用量汇总。
* `app/stage_runtime.py`：阶段共享执行逻辑。
* `app/stage_translation.py`、`app/stage_review.py`、`app/stage_terminology.py`：具体阶段行为。
* `app/llm_client.py`、`app/llm_response.py`：模型通信与响应解析。
* `app/project*.py`、`app/sqlite_storage.py`：项目生命周期、存储和导出。
* `app/term_*.py`：术语相关独立职责。
* `app/web.py`：Web 应用装配；`app/web_*_routes.py` 按资源职责提供接口。

详细模块边界维护在 `docs/MODULES.md`。新增或移动职责时更新该文档，不在其他文档重复维护模块清单。

## 4. 文档职责

每项事实应只有一个主要文档所有者。其他文档可以提供面向其受众的简短说明和链接，但不得复制完整协议、字段清单、算法或实现规则。

| 文档                    | 职责                                            |
| --------------------- | --------------------------------------------- |
| `README.md`           | 项目简介、能力、快速开始、主要限制和文档入口                        |
| `docs/USER_GUIDE.md`  | 用户操作方法及用户可观察行为                                |
| `docs/MINIMAL.md`     | 稳定产品语义、核心不变量、阶段行为和产品边界                        |
| `docs/ADAPTERS.md`    | LLM Adapter、Document Adapter、Preset、插件及协议版本契约 |
| `docs/MODULES.md`     | 代码模块职责、实现边界和依赖方向                              |
| `docs/DEVELOPMENT.md` | 开发环境、调试、测试、构建和打包                              |
| `docs/ROADMAP.md`     | 尚未实现的方向、进入条件和产品取舍                             |
| `AGENTS.md`           | Agent 与贡献者的仓库级工作规则                            |

文档修改遵循以下规则：

* 已实现行为不得继续作为完整规格保留在 `ROADMAP.md`。
* 用户指南只描述用户需要知道的结果，不展开内部存储字段和算法。
* Adapter 或插件协议只在 `ADAPTERS.md` 定义，其他文档引用即可。
* 工程原则只在 `AGENTS.md` 维护，`DEVELOPMENT.md` 不复制完整规则。
* 模块拆分只在 `MODULES.md` 维护。
* 若同一变更需要在多份文档中加入近似段落，应先检查是否发生职责重复。

不得因为“保持文档同步”而复制同一事实；应明确唯一权威来源。

## 5. 开发与验证

使用 Python 3.11+。Python 代码使用四空格缩进、UTF-8、公共函数类型标注；函数和模块使用 `snake_case`，类使用 `PascalCase`。

常用验证：

```bash
python -m pip check
python -m pytest -q
python -m app.main --help
python -m app.web --help
npm run typecheck --prefix web
npm run build --prefix web
git diff --check
```

根据修改范围执行必要验证：

* 后端行为变更：运行相关测试，合并前优先运行完整 Python 测试。
* 日常 `python -m pytest -q` 默认排除 `packaging` 标记探针；显式运行方式与范围见[开发指南](docs/DEVELOPMENT.md)。
* Web 变更：运行 TypeScript 检查和前端构建，并验证受影响交互。
* Adapter、存储、恢复或协议变更：必须覆盖对应契约和回归测试。
* 纯文档变更：检查链接、命令、标题层级和 `git diff --check`。

测试不得调用真实模型；使用确定性的模拟响应。

## 6. 配置与安全

不得提交：

* API Key、密码或其他凭据；
* 用户源文、生成项目和运行数据；
* 未脱敏请求或 Debug payload；
* `dist/`、`sidecar-dist/`、`src-tauri/target/` 等构建产物。

凭据只通过明确配置的环境变量或系统钥匙串读取，不写入 TOML、Preset、Prompt、Run 快照或日志。

不支持的配置、协议版本和运行环境应明确失败，不猜测、不自动修复、不静默兼容。

## 7. Git 与变更范围

保持 `dev` 可用。较大或独立工作应使用聚焦分支，例如：

```bash
git switch dev
git switch -c feat/segment-recovery
```

使用聚焦分支时，必须使用`--no-ff`合并

```bash
git merge --no-ff feat/segment-recovery
git branch -d feat/segment-recovery
```

使用 `feat/`、`fix/`、`docs/`、`test/` 等清晰前缀和 Conventional Commit 风格。

每个提交应：

* 对应一个可独立说明和验证的变更；
* 不包含无关重构；
* 记录实际执行的验证；
* 保持当前 Git 用户身份；
* 添加：

```text
Co-authored-by: Codex <codex@openai.com>
```

新增公共 API、配置项、依赖、文件、抽象层或兼容分支前，应能说明当前需求为何不能通过更小改动完成。
