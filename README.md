# Another LLM Translator

Another LLM Translator 是一个在本机运行、支持中断恢复的 LLM 文档翻译工作台。
它将文档导入、术语整理、翻译、校对、润色、人工审阅和导出整合在同一流程中，
主要通过本地 Web 界面使用，也提供 macOS 桌面壳和 CLI。

> [!WARNING]
> **Vibe Coding 声明**
>
> 本项目由 AI 协作驱动开发。请自行验证翻译质量、模型端点兼容性以及源文和凭据的安全性。
> 模型生成的翻译、校对和润色结果都应经过人工审阅。

## 可以做什么

- 导入 TXT、EPUB，以及安装 SRT 插件后可用的 SRT 文档，并按原文件分别管理和导出。
- 扫描、编辑和交换术语表，并可显式生成自动术语决策草案供用户逐条裁决。
- 分阶段执行翻译、校对和润色，明确选择是否应用修改建议。
- 以 Segment 为单位保存进度；中断后可继续处理未完成内容。
- 支持 OpenAI-compatible、OpenAI Responses、Gemini 和 Anthropic 等请求与响应格式。
- 可为不同阶段选择不同的 LLM Preset。
- LLM Preset 可按需开启 SSE 流式请求；只在完整响应通过解析和校验后保存结果，断流会丢弃并重试。
- 在 Web 中管理项目、模型连接、Prompt、术语、译文、诊断信息和导出文件。

当前版本为 `0.3.0`。本应用面向本机和可信局域网，不是远程、多用户或公网翻译服务。

## 通过本地 Web 开始使用

需要 Python 3.11 或更高版本，以及用于构建界面的 Node.js/npm。从源码目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --prefix web
npm run build --prefix web
python -m app.web
```

需要在源码 Web 中使用 SRT 时，再安装独立插件：

```bash
python -m pip install -e . -e plugins/srt
# 可选：安装术语使用校验示例插件
python -m pip install -e plugins/term_validation
```

官方桌面构建会在构建时装配该插件；已发布桌面应用暂不支持运行时安装任意插件。
术语使用校验插件同样在官方桌面构建时装配，但默认关闭；它只提供一次建议级修复，
仍未采用推荐译名时保留译文并记录 warning。

本发行版只识别 Another LLM Translator 的包名、命令、环境变量和数据目录。旧版本位置中的
数据不会自动发现、迁移或删除；如需保留，请用户自行处理。显式设置
`ANOTHER_LLM_USER_ROOT` 时，以该目录为准。

Windows PowerShell 使用：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm ci --prefix web
npm run build --prefix web
python -m app.web
```

启动后在浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。如需更换端口，使用 `python -m app.web --port PORT`。

### 首次使用

1. 打开右上角“设置”，在全局设置中选择或编辑 LLM Preset。
2. 为 Preset 指定模型、端点和凭据引用。密钥可以来自环境变量，也可以通过设置页保存到
   系统钥匙串。不要把 API Key 写进 Preset 或项目文件。
3. 在全局配置中确认新项目的默认目标语言，然后新建项目并添加 TXT、EPUB 或已安装插件
   支持的文件。已有项目可在项目设置中单独修改目标语言。
4. 根据需要依次进入“术语”“翻译”“校对”“润色”。每个阶段都可以先确认范围和已有结果的处理方式。
5. 校对和润色只生成建议，不会自动覆盖译文。审阅后明确应用需要的建议。
6. 在“导出”中选择结果阶段、文件范围、原格式或 TXT，以及是否生成双语版本。

完整的界面操作、凭据配置、恢复规则、格式说明和 CLI 用法见[用户指南](docs/USER_GUIDE.md)。

## macOS 桌面壳

仓库包含 macOS Tauri 桌面壳，它会在应用窗口中启动同一套本地 Web 工作台，并提供原生文件选择和导出位置选择。

当前仓库没有可直接下载的 GitHub Release，桌面应用需要从源码构建。
构建环境、开发运行和打包步骤见[开发指南](docs/DEVELOPMENT.md)。
Windows 与 Linux 桌面版本尚未公开提供。

## 数据、安全与限制

- 项目、设置和日志默认保存在平台用户数据目录；macOS 路径为 `~/Library/Application Support/another-llm-translator/`。
- API Key 只从显式配置的环境变量或系统钥匙串读取。普通日志不保存完整 Prompt、
  源文或鉴权 Header；Debug 模式可能保存敏感请求内容。
- Web 默认只允许本机访问。局域网共享必须在设置中显式开启，目前使用 HTTP，不适合公网暴露。
- 基础安装支持 TXT 和 EPUB；SRT 由独立可信插件提供。目前版本不支持 PDF、DOCX、Markdown 或任意格式互转。
- 应用不提供自动翻译质量评分或质量保证，关键内容必须人工检查。

## CLI（高级用法）

CLI 与 Web 使用相同的项目数据和执行逻辑。最小示例：

```bash
python -m app.main init novel.txt --name novel
python -m app.main run-all novel
python -m app.main export novel --stage translated --bilingual
```

`run-all` 不会自动应用校对或润色建议。完整命令、分阶段执行、范围选择、术语交换和恢复选项，
见[用户指南的 CLI 章节](docs/USER_GUIDE.md#8-cli高级用法)。

## 进一步阅读

- [用户指南](docs/USER_GUIDE.md)：Web、桌面、项目工作流、数据安全和 CLI。
- [开发指南](docs/DEVELOPMENT.md)：开发环境、前端与桌面调试、打包和验证。
- [MVP 规范](docs/MINIMAL.md)：已实现行为、数据模型和产品边界。
- [Adapter 契约](docs/ADAPTERS.md)：LLM Adapter、Document Adapter 和 Preset 契约。
- [产品路线图](docs/ROADMAP.md)：当前阶段和后续方向。

## 许可证

MIT
