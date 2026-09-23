# 模块职责

本文是 Another LLM Translator 实现架构、模块 ownership 和依赖方向的唯一文档。稳定产品
语义见[最小产品规范](MINIMAL.md)，Adapter 与插件接口见[Adapter 契约](ADAPTERS.md)，开发
命令见[开发指南](DEVELOPMENT.md)。精确数据库字段、迁移步骤和内部数据类以代码与迁移测试为准，
不在其他文档复制。

## 1. 仓库分区

- `app/`：Python CLI、Web API、领域执行、存储、Adapter 宿主与导出。
- `web/`：React/Vite/TypeScript 前端，只通过 HTTP API 使用后端能力。
- `src-tauri/`：Tauri 2 桌面壳、managed Python Web 进程生命周期和原生选择器。
- `config/`、`prompts/`、`llm_adapters/`、`llm_presets/`：随应用分发的内置资源。
- `plugins/`：可独立构建的可信 Python 示例插件。
- `tests/`：使用临时项目和模拟模型响应的契约与工作流测试。
- `packaging/`、`scripts/`：PBS 与依赖 lock、managed runtime、前端和桌面构建/探针入口。
- `docs/`：产品、协议、模块、开发、使用和路线图文档；各自职责由根目录
  [`AGENTS.md`](../AGENTS.md) 定义。

运行数据与构建产物不属于源码模块，不得作为模块间接口。

## 2. 依赖方向

主要依赖方向为：

```text
CLI / Web / Tauri
        ↓
项目与阶段入口
        ↓
阶段实现 → 执行调度 → LLM 通信
        ↓            ↘
术语 / 文档规则      项目存储
```

- `app/main.py` 和 Web route 只负责入口解析、权限与响应装配，不实现第二套阶段规则。
- 具体阶段模块调用共享运行时和执行模块；共享模块不得反向依赖 CLI 或 Web。
- 项目与存储模块拥有持久状态；Chunk、HTTP Attempt 和 Web 任务对象不是持久业务状态。
- Document Adapter 通过 `app/plugin_api.py` 的公开协议和 `app/plugins.py` 的宿主发现进入宿主，不直接操作项目数据库、
  Run 或正式输出目录。
- 前端只呈现服务端状态并提交用户决定，不推导 Segment 进度、结果复用或恢复结论。

## 3. 入口与应用装配

### `app/main.py`

CLI 命令注册、参数解析、交互确认和结果输出。它调用项目、阶段和导出模块，不拥有业务状态。

### `app/web.py`

FastAPI 应用装配、鉴权、生命周期、静态资源和 route 注册。全局依赖通过明确的构造与 route
注册传入，不把资源、项目、任务和导出逻辑重新集中到此文件。

### `app/web_*_routes.py`

按资源职责处理 HTTP 边界：

- `web_resource_routes.py`：全局配置、Prompt、Adapter、Preset、凭据和服务设置。
- `web_project_routes.py`：项目创建、打开、删除、File 管理与替换。
- `web_segment_routes.py`：Segment 浏览、编辑和阶段结果重置。
- `web_term_routes.py`：术语扫描、编辑、交换、发布与自动决策入口。
- `web_summary_routes.py`：内容概括选择、运行、聚合、阅读和 Markdown 导出。
- `web_task_routes.py`：阶段任务启动、状态、诊断和取消。
- `web_continuous.py`：连续运行阶段区间、预检快照、阶段选项摘要和运行编排；复用共享阶段入口，不拥有第二套阶段语义。
- `web_export_routes.py`：项目导出、下载和桌面保存位置。
- `web_storage_routes.py`：存储占用查询以及需要明确确认的可清理项入口。

Route 只校验 HTTP 输入并调用共享后端。请求模型集中在 `web_payloads.py`；Web 进程内项目缓存
和打开状态位于 `web_store.py`；任务生命周期位于 `web_tasks.py`。

## 4. 项目、存储与导出

### `app/project.py`

项目生命周期与聚合入口：初始化、加载、File 集合变更、项目配置/Prompt 边界以及调用存储层。
它不实现具体模型阶段。

### `app/sqlite_storage.py`

项目数据库的唯一持久化实现，负责 schema 初始化/迁移、事务、File/Segment、术语、阶段结果、
Run 索引和内容概括记录。调用者通过明确方法读写，不在 route 或阶段模块中直接拼接 SQL。

### `app/file_replacement.py`

单 File 替换的预览、保守内容对应和提交输入。它只判断哪些既有 Segment 身份可以安全保留；
实际事务写入仍由项目和存储模块完成。

### `app/project_export.py`

选择当前阶段结果、恢复宿主级文本规则、调用来源或 TXT Document Adapter、验证暂存输出并发布
导出文件。格式专属重建留在各 Document Adapter。

### `app/storage_management.py`

按需扫描用户数据根目录和已登记的项目路径，排除应用安装资源，按产品类别汇总占用并标记扫描
完整性。它只清理可再生的 DEBUG 附件、输出文件和日志，并在清理前检查项目写锁、活动任务和
运行中的 Run；不拥有 SQLite schema、阶段结果或项目生命周期。

### `app/locking.py`

项目写锁和冲突检测。所有项目写入口复用同一锁语义，不由 CLI、Web 或单个阶段各自实现。

## 5. 执行与阶段

### `app/execution.py`

拥有 Scope、Chunk 规划、Run 生命周期、Token/用量汇总、有限并发调度和取消传播。Chunk 只在
本次执行中存在；该模块不得用 Chunk 推断持久进度。

### `app/stage_runtime.py`

模型阶段共享运行逻辑，包括前置检查、设置差异处理、Prompt 装配、请求执行、响应归属和结果提交。
具体阶段规则不得堆回此模块。

### 具体阶段模块

- `stage_translation.py`：翻译 payload、结果校验、翻译校验与修复。
- `stage_review.py`：校对/润色的基准选择、accepted/suggested 结果，以及建议应用。
- `stage_terminology.py`：术语扫描、候选任务、联合片段概括和发布。
- `stages.py`：跨阶段公共入口、成功校验、完整状态检查和 `run-all` 编排；不承载各阶段算法。

### 内容概括

- `summary_aggregation.py`：按内容边界聚合片段、递归压缩和独立发布完整结果。
- `summary_provenance.py`：概括依赖构建与校验、过期原因和翻译上下文可用性判定。

内容概括复用阶段执行、LLM、存储和 Document Adapter 边界，不建立另一套项目或请求框架。

## 6. LLM 通信

### `app/llm_adapter.py`

声明式 JSON LLM Adapter 的加载、严格校验、模板渲染、响应指针和 SSE 规则。协议字段以
[Adapter 契约](ADAPTERS.md)为准。

### `app/llm_preset.py` 与 `app/llm_keys.py`

`llm_preset.py` 解析 Base URL、模型、限流和凭据引用；`llm_keys.py` 管理一次执行中的多 Key 选择、
限流、冷却和安全审计。二者不读取或写入阶段结果。

### `app/llm_client.py`

宿主 HTTP Client、普通/流式传输、超时、重试、取消、诊断与请求审计。Adapter 只描述 wire
转换，不能自行发送请求或绕过本模块。

### `app/llm_response.py`

规范化模型正文、严格 JSONL 解析、短请求 ID 校验和部分响应判定。具体字段条件由调用它的阶段
模块提供。

### `app/llm_migration.py`

用户级 Adapter/Preset schema 的显式启动迁移。Run 历史快照不在此静默改写。

## 7. 文档与插件

### `app/documents.py`

Document Adapter 协议、内置 TXT Adapter、导入/导出通用边界和 Adapter 选择。宿主分配
File/Segment 身份并保存通用记录，Adapter 只拥有格式解析与重建。

### `app/epub_adapter.py`

EPUB 的 ZIP/XML 安全校验、文本流提取、Ruby/内联格式模型表示和原格式重建。EPUB 专属规则
不得进入项目、阶段或通用 Document Adapter 模块。

### `app/plugins.py`

可信 Python 目录插件的 manifest 发现、独立模块加载、描述符和协议版本校验。它先检查官方
资源与用户数据目录的全部 manifest，再在同一进程中加载插件；成功的注册结果按进程缓存。插件
只能通过公开协议注册 Document Adapter 与 Translation Validator。

### `app/plugin_api.py`

可信 Python 插件使用的公开契约入口，集中导出 PluginDescriptor、Document Adapter 与翻译校验
类型、严格文本解码 API 和插件所需的宿主错误类型；不负责插件发现或业务执行。

### `app/translation_validation.py`

翻译校验协议、内置校验规则、finding 规范化和修复上下文。校验器不拥有 HTTP 重试或结果提交。

## 8. 术语

- `term_library.py`：术语规范化、候选合并、发布库、override 和组关系。
- `term_exchange.py`：JSON/CSV 交换格式的完整校验、导入与导出。
- `term_matching.py`：运行时逐 Segment 匹配和注入选择，不持久化 occurrence。
- `term_decision.py`：自动术语决策执行入口和阶段编排。
- `term_decision_batches.py`：批次规划与关联项分组。
- `term_decision_protocol.py`：模型输入输出的解析与严格校验。
- `term_decision_rules.py`：确定性决策、冲突和保护规则。
- `term_decision_drafts.py`：审查草案、应用、回滚和失效条件。

这些模块共享同一个已发布术语库。自动决策只能生成草案，不能绕过 `term_library.py` 的人工
覆盖和发布语义。

## 9. 配置、资源与诊断

- `config.py`：项目配置 schema、严格加载和规范写入。
- `user_config.py`：平台用户数据根与内置/用户资源覆盖路径。
- `prompt_library.py`：用户级 Prompt 条目及项目载入边界。
- `credentials.py`：环境变量和系统钥匙串访问，不向持久化层暴露密钥正文。
- `server_config.py`：监听、局域网共享与认证设置。
- `diagnostics.py`：本次运行的结构化诊断事件与摘要。
- `logging_utils.py`：普通日志上下文和敏感字段边界。
- `i18n.py`：CLI/后端可见文案与语言选择。
- `errors.py`：可预期应用错误的公共基类。

配置字段的用户含义写在[用户指南](USER_GUIDE.md)，协议字段写在[Adapter 契约](ADAPTERS.md)；
本文只维护由哪个模块负责。

## 10. 前端与桌面壳

`web/src/components/` 按页面或弹窗拆分。`AppShell.tsx` 只装配导航和全局状态；项目概览、
创建、选择、替换、输入、导出、诊断、Segment、术语、自动决策、概括和设置分别由对应组件
拥有。共享 API 类型位于 `web/src/types.ts`，通用选择行为位于 `useClassicSelection.ts`。

`StorageView.tsx` 负责设置页中的存储汇总、项目明细和逐项清理交互；它不复制后端扫描或安全判断，
清理后重新读取服务端状态。

页面局部 UI 状态留在对应 workspace/component；服务端拥有的项目、运行和结果状态必须重新
读取 API，不在前端建立权威副本。

`src-tauri/src/` 负责桌面窗口、从应用资源目录定位 bundled managed Python 并以
`-m app.web` 启动 Web 服务、管理该进程及原生选择器；开发构建从明确的 runtime 目录启动，
正式构建使用 app 内的 runtime。`packaging/` 与 `scripts/` 负责 runtime 组装、检查和 Tauri
打包；桌面壳不实现独立业务后端。

## 11. 变更规则

- 新增或移动职责时更新本文，并从其他文档删除重复模块清单。
- 精确字段和算法优先由类型、迁移和测试表达；只有影响跨模块依赖时才在本文记录。
- 工程取舍和提交要求统一见 [`AGENTS.md`](../AGENTS.md)，不在本文重复。
