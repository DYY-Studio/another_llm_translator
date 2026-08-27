# 开发指南

本文面向从源码开发、调试或打包 Another LLM Translator 的贡献者。一般使用方法见
[用户指南](USER_GUIDE.md)，完整产品行为以 [MVP 规范](MINIMAL.md)为准。

## 1. 环境准备

运行时要求 Python 3.11 或更高版本。前端开发需要 Node.js/npm；桌面壳开发和打包还需要
Rust/Cargo、Tauri 2 工具链及对应平台构建工具。

创建 Python 虚拟环境并安装开发依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip check
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pip check
```

`requirements.txt` 只包含运行时依赖；`requirements-dev.txt` 在此基础上增加测试和构建依赖。
API Key 必须通过 Preset 引用的环境变量或系统钥匙串提供，不要写入仓库文件。

开发依赖会以 editable 方式安装宿主、`plugins/srt` 和
`plugins/term_validation`，因此测试与桌面构建可以发现官方 SRT 与术语校验
entry point。两个插件都可以单独构建并安装；插件代码与宿主同进程运行，安装即表示信任。

## 2. 仓库结构

- `app/`：CLI、本地 Web API、项目存储、阶段执行、LLM 请求和导出。
- `web/`：React/Vite/TypeScript 前端源码。
- `src-tauri/`：Tauri 2 桌面壳、sidecar 编排和原生选择器。
- `config/`、`prompts/`、`llm_adapters/`、`llm_presets/`：随应用分发的内置资源。
- `tests/`：使用模拟 LLM 响应的确定性工作流测试。
- `packaging/`：冻结 Python/FastAPI sidecar 的 PyInstaller 配置。
- `plugins/srt/`：可单独构建和发行的 SRT Document Adapter 示例插件。
- `plugins/term_validation/`：可单独构建和发行的术语使用 Translation Validator 示例插件。
- `scripts/`：前端、sidecar 和桌面构建辅助脚本。
- `docs/`：产品规范、Adapter 契约、用户与开发文档。

`app/web_dist/`、`sidecar-dist/`、`src-tauri/target/` 和 `dist/` 是构建产物，
不应提交。`projects/` 和 `logs/` 是运行数据，也不应提交。

## 3. 核心概念和边界

项目使用四个职责不同的概念：

- **File**：源文档边界，也是选择和导出的边界。
- **Segment**：可翻译的持久化内容单元，是进度和恢复单位。
- **Chunk**：一次 LLM 请求的临时分组，不是业务状态。
- **Run**：一次阶段执行记录，保存范围、设置和执行摘要。

LLM 请求、Chunk 和参考上下文不会跨越不允许合并的 File 或 EPUB XHTML part。
不要用 Chunk 推断持久化进度，也不要混淆 File、Segment、Chunk 和 Run。

数据模型、指纹、恢复、阶段结果和验收行为详见 [MVP 规范](MINIMAL.md)。LLM Adapter、
Document Adapter、插件和 Preset 的契约详见 [Adapter 契约](ADAPTERS.md)。

## 4. Web 开发

安装前端依赖并执行检查：

```bash
npm ci --prefix web
npm run typecheck --prefix web
npm run build --prefix web
```

生产构建写入 `app/web_dist/`。该目录缺失时，FastAPI 仍会提供 API，但不会提供完整 Web 页面，并会记录警告。

构建前端后启动后端：

```bash
python -m app.web
```

默认地址为 `http://127.0.0.1:8765`，可以通过 `--port` 更换端口。服务监听和 LAN 放行由
服务配置与 HTTP 守卫共同控制；默认只允许回环客户端。

前端和 CLI 共用相同的项目数据库、阶段执行、限速、写锁和恢复代码。不要在前端实现第二套业务状态或恢复逻辑。

## 5. macOS 桌面开发与打包

### 开发运行

先完成 Python 依赖和前端构建，再运行：

```bash
npm run build --prefix web
bash scripts/desktop-dev.sh
```

`desktop-dev.sh` 设置仓库根目录和 Python 解释器后执行 Tauri 开发壳。可用环境变量：

- `ANOTHER_LLM_PYTHON`：开发模式使用的 Python，默认 `.venv/bin/python`。
- `ANOTHER_LLM_REPO_ROOT`：桌面开发壳使用的仓库根目录，由脚本自动设置。
- `ANOTHER_LLM_WEB_PORT`：sidecar Web 端口，默认 `8765`。

`scripts/build-sidecar.sh` 使用 PyInstaller 收集构建环境中已安装的
`another_llm_translator.plugins` entry point 及其发行元数据。官方构建会检查 SRT
和术语校验 entry point 已安装后再冻结；这提供构建时插件装配，不提供成品运行时
安装任意插件。

发布改名不保留旧包名、命令、环境变量或插件组。默认用户数据目录由旧名称迁移到
`another-llm-translator`：只有新目录不存在时才迁移；新目录存在则跳过且不覆盖旧目录。

桌面壳启动时优先拉起 bundle 内的冻结 sidecar；找不到时，使用开发环境中的
`python -m app.web`。健康探测成功后加载 `http://127.0.0.1:<port>`。退出桌面应用时，
会终止由本次进程启动的 sidecar。

如果应用异常退出后端口上仍有兼容服务，再次启动可能继续使用该服务。必要时应手动结束
残留进程。

桌面端通过 Tauri command 提供原生文件、文件夹和导出位置选择；普通浏览器仍使用上传、服务端目录浏览和下载。

### 打包 macOS 应用

`scripts/build-app.sh` 依次执行前端类型检查与构建、PyInstaller sidecar 冻结和 Tauri 打包：

```bash
bash scripts/build-app.sh
```

脚本生成未签名的 ad hoc `.app` 和 zip，输出目录为：

```text
dist/another-llm-translator-<版本>-macos-arm64/
```

构建产物内含配置、Prompt、Adapter、Preset 和 Web 静态资源。当前脚本面向 macOS arm64。
签名、公证和公开发行流程尚未建立，仓库也没有可直接下载的 GitHub Release。

本地安装时将 `.app` 拖入“应用程序”。未签名应用首次打开可能需要在
“系统设置 → 隐私与安全性”中放行，或通过右键菜单选择“打开”。升级和卸载应用都不会
自动删除平台用户数据目录中的项目与设置。

## 6. 配置、存储与安全

内置全局资源随源码或应用包分发。Web 对全局配置、Prompt、Adapter 和 Preset 的修改写入
平台用户数据根目录；同名用户资源优先于内置资源，内置文件本身保持只读。

macOS 默认用户数据根目录：

```text
~/Library/Application Support/another-llm-translator/
```

开发时可以用 `ANOTHER_LLM_USER_ROOT` 覆盖。典型项目目录包含：

```text
projects/<name>/
├── project.sqlite
├── config.toml
├── prompts/
├── input/
├── runs/
├── logs/
└── output/
```

用户级提示词仓库存放在同一用户数据根目录下，不属于任何项目：

```text
prompt_library/<stage>/<language>/<prompt-id>.middle.txt
```

`prompt-id` 只允许以小写字母开头并包含小写字母、数字和连字符。仓库条目使用
UTF-8 原子写入，按阶段和语言隔离；读取、覆盖或删除仓库条目不会修改全局 Prompt、
项目 Prompt 或项目元数据。仓库内容不进入 Bundle Hash、阶段指纹或 Run 快照，直到
用户将其载入项目编辑器并显式保存为项目 Prompt。

`project.sqlite` 是项目权威存储。Run 目录提供可读的 manifest 与设置快照，
但不能代替数据库判断进度。

普通日志不得记录完整 Prompt、源文、鉴权 Header、未脱敏请求正文或流式增量正文。
Debug 记录可能含敏感内容；启用时会保存每个流式 Attempt 收集到的原始 SSE
`data` 事件，只能用于明确的本地诊断。诊断 API 只返回流式事件数、接收字节数和
首事件耗时，完整正文仍须通过格式解析与校验后才进入请求详情。Document Adapter
和 LLM Adapter 插件是可信同进程扩展，不提供沙箱。

Preset schema 4 的 `stream` 必须由用户显式开启，且只对声明 `streaming` SSE
规则的 JSON LLM Adapter 有效。启动 CLI、Web 或桌面 sidecar 会先原子迁移用户
schema 2/3 Preset 和 schema 1 Adapter；迁移失败应终止启动，不留下兼容副本。流式
请求默认使用 `request_timeout_seconds` 作为连接及连续读取的空闲超时；关闭
`stream_read_timeout_enabled` 后只取消连续读取超时，不限制完整生成
时间；EOF、读取超时和流内错误会丢弃半成品并沿 HTTP 尝试次数重试，不自动回退为
非流式。

## 7. 验证

文档或代码变更提交前，根据影响范围运行：

```bash
python -m pip check
python -m pytest -q
python -m app.main --help
python -m app.web --help
npm run typecheck --prefix web
npm run build --prefix web
git diff --check
```

测试使用临时项目和模拟 HTTP 响应，不应调用真实模型。若只修改 Markdown，可以跳过应用
测试和前端构建，但仍应检查链接、命令、标题层级和 `git diff --check`。

## 8. 实现原则

- 以当前明确需求为目标，优先最小、直接、易维护的实现。
- 只在系统边界校验外部输入，不为假设中的未来功能增加扩展框架或兼容分支。
- 设置变化、术语冲突、结果复用和建议应用等关键决策必须由用户明确作出。
- 不添加无明确触发条件的 fallback、自动重试、feature flag 或静默降级。
- 保留鉴权、数据保护、注入防护、项目写锁和持久化一致性等必要安全属性。
- 修改后做减法审查，删除未使用代码、重复校验和不必要分支。

更完整的仓库协作、测试和提交要求见根目录的 [`AGENTS.md`](../AGENTS.md)。
