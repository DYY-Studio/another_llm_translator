# Another LLM Translator

Another LLM Translator 是一个面向本地使用的、可恢复的 LLM 工程化翻译工作台。
它将文档导入、术语管理、翻译、校对、润色、人工审校和文档导出组织为一条可追踪
的工作流，同时提供 CLI 和本地 Web 两种操作方式。

> **Vibe Coding 声明**
>
> 本应用是完全 Vibe Coding 实现的项目。代码、测试、界面和文档均在 AI 协作驱动
> 下完成。实际使用时，请自行验证翻译质量、模型端点兼容性以及源文和凭据的安全性。

## 项目状态

- 当前版本：`0.3.0`，对应 MVP 0.3 实现。
- 当前形态：单机本地 CLI 和 Web Alpha。
- 当前路线：Stage 1 至 Stage 20 已完成，后续路线见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。
- 当前发布方式：从源码目录运行，或 `pip install .` 后使用两个 console script
  （`minimal-llm-translator`、`minimal-llm-translator-web`）。

本项目默认不面向远程部署、公网服务。Web 默认只监听本机回环地址，并拒绝非本机
Host 和跨站 Origin；局域网共享需要用户显式开启接口并可选开启登录认证。

## 核心流程

```text
TXT/EPUB 导入
    -> Segment 化
    -> 术语提取
    -> 翻译
    -> 校对建议
    -> 可选应用校对
    -> 润色建议
    -> 可选应用润色
    -> 单语或双语导出
```

`run-all` 会依次执行术语提取、翻译、校对和润色，但不会自动应用校对或润色建议。
校对和润色结果均需要用户明确决定是否应用。

## 主要能力

- 支持 TXT 和 EPUB 两种内置文档格式。
- 使用 SQLite 保存项目元数据、File、Segment、术语、阶段结果和 Run 索引。
- 以 Segment 作为进度和恢复单位，已完成结果默认复用，失败或未完成 Segment 可继续处理。
- 支持术语扫描、人工冲突裁决、启用/移除术语，以及 JSON/CSV 导入导出。
- 支持翻译、校对建议、润色建议和独立的 apply 结果，不覆盖原始阶段历史。
- 支持同一项目为术语、翻译、校对和润色分别指定 LLM Preset。
- 支持声明式 JSON LLM Adapter、上下文、动态分块、并发、RPM/ITPM 限速和有限重试。
- 支持模型发现和端点精确 usage 汇总；端点未完整返回 usage 时不会使用本地估算冒充实际消耗。
- CLI 与 Web 共用项目存储、阶段执行、写锁、限速和恢复逻辑。
- Web 支持项目创建、打开、删除、文件管理、配置编辑、Prompt 编辑、Preset 和 Adapter 管理、
  Segment 审校、批量 apply、导出、术语工作区和诊断面板。

## 核心概念

项目中的四个核心概念具有不同职责，不应混用：

- **File**：文档文件边界，也是文件选择和导出的边界。
- **Segment**：可翻译的有序内容单元，是阶段进度、结果和恢复的持久化单位。
- **Chunk**：当前 LLM 请求的临时分组，不是持久化业务状态，也不能用于判断进度或恢复。
- **Run**：一次阶段执行记录，保存范围、配置、Prompt、Preset/Adapter 快照和执行摘要。

LLM 请求、Chunk 和参考上下文不会跨越不同 File 或 EPUB 的不同 XHTML part。

## 环境要求

- Python 3.11 或更高版本。
- Node.js/npm 仅在需要从 Web 源码重新构建前端资源时使用。
- 可访问所配置 LLM 端点的网络环境。

## 安装与配置

当前推荐从源码目录运行。创建虚拟环境并安装开发依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip check
```

Windows PowerShell 可使用：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pip check
```

`requirements.txt` 包含运行时依赖；`requirements-dev.txt` 在此基础上增加测试和构建依赖。

### 配置 LLM Preset

项目配置只保存命名 Preset，不保存内联连接配置。全局 Preset 位于
`llm_presets/<preset_id>.json`，全局配置位于 `config/config.toml`。这些内置资源
只读；用户在 Web 中修改的全局配置、Prompt、Adapter 和 Preset 会写入平台用户数据
根目录（macOS `~/Library/Application Support/minimal-llm-translator`，可用
`MINIMAL_LLM_USER_ROOT` 环境变量覆盖），同名文件优先于内置资源读取。

首次使用前至少需要完成以下步骤：

1. 编辑 `llm_presets/default.json`，将示例端点、模型和限流参数改为实际值。
2. 确认 Preset 的 `credential` 引用：`{"kind": "environment", "name": "LLM_API_KEY"}`
   指定环境变量，或 `{"kind": "keychain", "name": "<凭据 ID>"}` 指向系统钥匙串；
   两种引用二选一，不隐式回退。然后设置对应的环境变量或写入钥匙串。
3. 确认 `config/config.toml` 中的 `llm.preset` 指向所选 Preset。

例如，使用 OpenAI-compatible 端点时：

```bash
export LLM_API_KEY="your-api-key"
```

不要把 API Key 写入 TOML、Preset、Prompt、项目文件、Run 快照或日志。Preset
只保存凭据引用，不保存密钥本身。

仓库中的内置 Preset 是可编辑的示例：

| Preset | Adapter | 当前示例凭据引用 | 说明 |
| --- | --- | --- | --- |
| `default` | `openai-compatible` | `LLM_API_KEY` 环境变量 | 默认指向示例端点，必须修改后使用 |
| `google-gemini` | `google-gemini` | `GEMINI_API_KEY` 环境变量 | Gemini 原生 JSON 端点示例 |
| `openai-responses` | `openai-responses` | `OPENAI_API_KEY` 环境变量 | OpenAI Responses API 示例 |
| `anthropic-claude` | `anthropic` | `ANTHROPIC_API_KEY` 环境变量 | Anthropic Messages API 示例 |

项目可以在 `config.toml` 的 `llm.preset_terminology`、
`llm.preset_translation`、`llm.preset_proofreading` 和
`llm.preset_polishing` 中为单个阶段指定覆盖 Preset。空字符串表示继承全局 Preset。

声明式 Adapter 的完整字段、消息格式、响应 JSON Pointer、模型发现、usage 映射和
`extra_body` 规则见 [`docs/ADAPTERS.md`](docs/ADAPTERS.md)。当前声明式 LLM Adapter
只支持非流式 JSON POST；不会自动判断 Provider、自动切换端点或静默 fallback。

如需显式代理，可在 Preset 中使用 HTTP/HTTPS `proxy_url`。留空时，HTTPX 仍可按标准
环境变量使用代理。

## CLI 使用

### 快速开始

下面的示例以 TXT 文件和目标语言为简体中文为例：

```bash
python -m app.main init novel.txt --name novel
python -m app.main run-all novel
python -m app.main inspect novel
python -m app.main apply novel --stage proofreading --all
python -m app.main apply novel --stage polishing --all
python -m app.main export novel --stage polished --bilingual
```

上例中的润色建议在 `run-all` 内默认以翻译结果为基准，因为 `run-all` 不会隐式应用校对。
如果希望润色以已应用的校对结果为基准，请分阶段执行：

```bash
python -m app.main terminology novel
python -m app.main translate novel
python -m app.main proofread novel
python -m app.main apply novel --stage proofreading --all
python -m app.main polish novel
python -m app.main apply novel --stage polishing --all
python -m app.main export novel --stage polished
```

### 创建与管理项目

项目默认创建在用户数据根目录的 `projects/<name>/` 下（可用 `MINIMAL_LLM_USER_ROOT`
覆盖），也可以通过 `--parent-dir` 在明确的父目录创建，或直接使用项目绝对路径打开
已有项目：

```bash
python -m app.main init novel.txt --name novel
python -m app.main init novel.txt --recursive --name novel
python -m app.main init --empty --name novel
python -m app.main init --empty --name novel --parent-dir /absolute/parent
python -m app.main files-add novel chapter-1.txt chapter-2.txt
python -m app.main files-add novel appendix.epub
python -m app.main files-remove novel F0001
```

空项目不会预先锁定文档格式，可以后续追加受支持的不同格式文件。运行中的项目不能增删文件。
移除文件会使其不再参与统计、复用和导出，但不会自动清理历史阶段记录和既有输出。

EPUB 初始化时需要明确指定 Adapter：

```bash
python -m app.main init book.epub \
  --name book \
  --document-adapter epub \
  --adapter-option epub.ruby_mode=aozora
```

任意 Document Adapter 的导入与运行选项都通过可重复的
`--adapter-option ADAPTER.OPTION=VALUE` 传入（如 EPUB Ruby 模式
`aozora`、`base_only` 和 `parenthetical`）。导入选项会固化在该 File
的 Adapter 状态中；修改选项需要移除并重新导入文件。

### 阶段与范围

可用的主要命令包括：

- `init`：创建项目。
- `files-add`、`files-remove`：追加或移除活动源文件。
- `inspect`：检查项目状态、结果和设置指纹。
- `terminology`：扫描并发布术语。
- `translate`：执行翻译。
- `proofread`：生成校对建议。
- `polish`：生成润色建议。
- `run-all`：依次执行完整建议流程，不自动 apply。
- `apply`：应用校对或润色建议。
- `export`：导出翻译结果。
- `terms-import`、`terms-export`：交换 JSON/CSV 术语表。
- `terms-publish-partial`：发布当前活动扫描中已经产生的术语候选。

翻译、校对和润色阶段支持按文件或 Segment 选择范围：

```bash
python -m app.main translate novel --only-file F0001
python -m app.main translate novel --only-segment F0001-S000001
python -m app.main translate novel --from-file F0002
```

阶段命令还支持：

- `--dry-run`：只报告范围、Chunk 数、Token 估算和必要的选择，不写入项目、不创建 Run、不调用 LLM。
- `--force`：明确要求重做选定范围内已有的 completed 结果。
- `--reuse-mixed-fingerprints`：显式复用设置指纹不同的已完成结果。
- `--resume-run`、`--decline-run`：在非交互环境中明确处理同阶段未完成 Run。

CLI 默认输出 JSON 摘要，日志写入标准错误和项目 `logs/app.log`。可以使用
`--language system`、`--language zh-CN` 或 `--language en` 选择界面语言；
Run 的提示词语言跟随该选择（缺失的语言视图回退 `zh-CN`）。

### 术语交换

```bash
python -m app.main terms-export novel glossary.json
python -m app.main terms-export novel glossary.csv
python -m app.main terms-import novel glossary.json
python -m app.main terms-export novel scanned.json --source scanned
python -m app.main terms-publish-partial novel
```

术语冲突不会被静默裁决。`terms-import` 会在完整校验后合并，人工 override 优先于自动扫描结果。

### 导出

```bash
python -m app.main export novel --stage translated
python -m app.main export novel --stage proofread --bilingual
python -m app.main export novel --stage polished --format txt
python -m app.main export novel --stage translated --file F0001
```

`translated`、`proofread` 和 `polished` 分别对应翻译、已应用的校对和已应用的润色结果。
原格式导出按每个 File 的来源 Adapter 重建；使用 `--format txt` 时统一通过 TXT Adapter 导出。
导出不支持跨 File 合并或按 Segment 单独导出；跨 File 合并只适用于启用配置的 LLM
Chunk 请求规划。

## 本地 Web

仓库已包含可运行的 Web 静态资源。如需从前端源码重新构建：

```bash
cd web
npm ci
npm run typecheck
npm run build
cd ..
```

启动本地 Web：

```bash
python -m app.web
```

默认访问地址为 `http://127.0.0.1:8765`。也可以通过 `--port` 修改端口；省略
`--host` 时按用户数据根目录的 `server.toml` 决定绑定地址（默认 `127.0.0.1`）。

Web 与 CLI 共用同一项目目录、SQLite 存储、阶段执行、限速、写锁和恢复逻辑。Web 支持：

- 创建、打开、删除项目，以及打开外部绝对路径项目。
- 上传、追加和移除 TXT/EPUB 文件。
- 编辑项目配置、项目 Prompt、全局配置、全局 Prompt、LLM Preset 和 JSON Adapter；
  全局编辑写入用户数据根目录，内置资源只读、不可删除，同名用户文件优先。
- 手动检测模型列表、查看脱敏请求预览和检查诊断信息。
- 运行、取消和恢复阶段任务；Run 的提示词语言跟随界面语言，设置页可预览装配后
  的完整提示词。
- Segment 审校、批量 apply、单语/双语导出和文件范围选择。
- 术语搜索、冲突裁决、编辑、移除、彻底删除和 JSON/CSV 交换。
- 在设置页管理系统钥匙串凭据；LLM Preset 通过 `environment` 或 `keychain`
  显式单凭据引用获取密钥。
- 在设置页显式开启指定局域网接口的共享：开启认证后 LAN 客户端需通过登录页和
  HttpOnly 会话 Cookie 访问，密码存入系统钥匙串，服务重启或停止共享后会话失效、
  长期账密保留；未开启认证时界面持续警告同网段设备拥有完整操作权限。

Web 默认只监听回环地址并限制 Host 和 Origin，适合可信的本机用户。局域网共享
需要显式开启，首版使用 HTTP，不提供 TLS、多账号、角色、密码找回或公网访问。

## 输入与输出范围

### TXT

- 支持 `.txt` 和 `.text` 文件。
- 支持显式文件、目录和递归目录导入。
- 支持 BOM 识别、编码探测、GBK/GB2312 到 GB18030 的映射和一次严格 fallback。
- 保留逻辑行、空行和 Segment 顺序。
- 不承诺原始字节、原始换行符、BOM 或输入编码完全往返。

### EPUB

- 支持 OPF 2.0/3.0 和 spine XHTML。
- 一个 EPUB 对应一个 File，每个 spine XHTML 在 File 内使用独立 part 边界。
- 保留导航、元数据、图片、CSS、字体和其他未翻译资源。
- 支持 Ruby 导入模式，以及受控的普通内联格式处理。
- 纯译文和双语导出由 EPUB Adapter 重建，不把模型输出当作任意 HTML 直接写入文档。

### 阶段结果

- 翻译结果以 Segment 为单位保存。
- 校对和润色结果区分 `accepted` 与 `suggested`。
- `accepted` 表示保留当前基准；`suggested` 保存建议文本和原因。
- `apply` 会写入独立的 applied 结果，不覆盖翻译、校对或润色历史。
- 空 Segment 保留在项目中，但不会调用 LLM，也不要求阶段结果。

当前不支持 PDF、DOCX、Markdown 或任意格式互转，也不提供自动翻译质量评分。

## 持久化与恢复

典型项目目录如下：

```text
projects/<name>/
├── project.sqlite       # 项目权威存储
├── config.toml          # 项目配置副本
├── prompts/             # 项目 Prompt 副本
├── input/               # 源文件副本
├── runs/                # Run manifest 和配置快照
├── logs/                # 项目日志
└── output/              # 导出结果
```

`project.sqlite` 是项目元数据、File、Segment、术语、阶段结果、活动任务状态和 Run 索引的
权威存储。Run 目录保留可读的 `manifest.json`、配置、Prompt、Preset 和 Adapter 快照。

恢复规则如下：

- 有 completed 结果的 Segment 默认复用。
- 没有成功结果的 pending 或 failed Segment 会继续处理。
- `--force` 才会重做已有 completed 结果。
- 独立阶段可以续用最近的同阶段未完成 Run；`run-all` 不支持 `--resume-run`。
- 恢复依据是 Segment 结果和术语扫描状态，不依据 Chunk 或单个 HTTP 请求状态。
- 设置、Prompt、Preset 或 Adapter 变化不会自动清空旧结果；是否复用由用户明确决定。
- 修改源文不支持增量更新，通常需要重新创建项目。
- 当前 SQLite schema 为版本 1；旧 JSONL 项目不自动迁移，必须重新创建。

普通写操作通过项目写锁互斥。不要手工修改 `project.sqlite` 或项目 `input/` 内容。

## 安全与隐私

- API Key 只从环境变量读取，不进入 URL、请求正文、Run 快照、阶段指纹或普通日志。
- 普通日志不会保存完整 Prompt、源文、鉴权 Header 或未脱敏 Payload。
- Debug 模式会额外保存完整请求、响应、Attempt 和 Chunk 信息，其中可能包含 Prompt 与源文。
  处理敏感资料时不要启用 Debug 模式。
- Web 仅绑定回环地址，并限制 Host 和 Origin。
- Document Adapter 插件如被安装，会与宿主在同一进程运行，拥有当前进程权限，不提供沙箱。

## 明确限制

- 不支持远程、多用户、LAN 或公网部署。
- 不提供 TLS、登录认证、系统钥匙串或桌面应用。
- 不支持流式 LLM、Python LLM Adapter、自动 Provider 判断或静默 Provider fallback。
- 不支持 PDF、DOCX、Markdown 等通用文档转换。
- 不提供自动质量评分或质量保证；模型结果仍需人工审阅。
- 术语冲突需要人工裁决，强制重新扫描不会自动删除未再次发现的术语。
- 内置资源保持只读；用户内容写入用户数据根目录，删除内置资源在 Web 中明确失败。

## 验证与开发

运行完整测试和基础入口检查：

```bash
python -m pip check
python -m pytest -q
python -m app.main --help
python -m app.web --help
cd web
npm run typecheck
npm run build
cd ..
```

测试使用临时项目和模拟 HTTP 响应，不会调用真实模型。测试和开发边界见
[`AGENTS.md`](AGENTS.md)。

## 进一步阅读

- [`docs/MINIMAL.md`](docs/MINIMAL.md)：已实现的 MVP 行为、数据模型和边界。
- [`docs/ADAPTERS.md`](docs/ADAPTERS.md)：LLM Adapter、Document Adapter 和 Preset 契约。
- [`docs/ROADMAP.md`](docs/ROADMAP.md)：产品阶段、后续规划和明确不建设的能力。

## 许可证

MIT
