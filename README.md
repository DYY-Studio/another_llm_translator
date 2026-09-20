# Another LLM Translator

Another LLM Translator 是一个面向本机和可信局域网使用、支持中断恢复的 LLM 文档翻译工作台。它把文档
导入、术语整理、翻译、校对、润色、人工审阅和导出放在同一流程中，主要通过本地 Web 使用，
也提供 macOS 桌面壳和 CLI。

> [!WARNING]
> **Vibe Coding 声明**
>
> 本项目使用 AI 协作开发。使用前请验证模型端点兼容性与敏感数据处理；翻译、校对和润色结果需人工审阅。

## 可以做什么

- 导入 TXT、EPUB 和随应用提供的 SRT 插件支持的 SRT 文档，并按原文件分别管理和导出。
- 扫描、编辑、交换和发布术语；自动术语决策只生成待人工审批的草案。
- 分阶段执行翻译、校对和润色，明确选择是否应用建议。
- 以 Segment 为单位保存进度；取消、失败或重启后可继续未完成内容。
- 连接 OpenAI-compatible、OpenAI Responses、Gemini 和 Anthropic 等请求格式。
- 为不同阶段选择不同 LLM Preset，并显式选择流式请求。
- 在 Web 中管理项目、Prompt、术语、译文、诊断和导出文件。

当前版本为 `0.3.0`。

## 5 分钟开始使用

需要 Python 3.11+ 和 Node.js/npm。在源码目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --prefix web
npm run build --prefix web
python -m app.web
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm ci --prefix web
npm run build --prefix web
python -m app.web
```

启动后打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，然后：

1. 在“设置”中选择或编辑 LLM Preset，选择 Adapter，并填写 Base URL、模型和凭据引用；请求路径由 Adapter 定义。
2. 把 API Key 放在 Preset 引用的环境变量或系统钥匙串中，不要写入配置文件。
3. 确认新项目的默认目标语言，新建项目并添加文档。
4. 按需执行“术语 → 翻译 → 校对 → 应用 → 润色 → 应用”。
5. 在“导出”中选择结果阶段、文件范围、格式和单语/双语输出。

完整的界面操作、插件安装、凭据配置、恢复行为和格式说明见[用户指南](docs/USER_GUIDE.md)。

## CLI 最小示例

CLI 与 Web 使用相同的项目和执行语义：

```bash
python -m app.main init novel.txt --name novel
python -m app.main run-all novel
python -m app.main export novel --stage translated --bilingual
```

`run-all` 不会自动应用校对或润色建议。分阶段执行、范围选择、术语交换和恢复选项见
[用户指南的 CLI 章节](docs/USER_GUIDE.md#8-cli高级用法)。

## macOS 桌面壳

仓库包含 Tauri 桌面壳，它在应用窗口中运行同一套本地 Web 工作台，并提供原生文件和导出
位置选择。当前没有可直接下载的 GitHub Release，需要从源码构建；开发运行和打包步骤见
[开发指南](docs/DEVELOPMENT.md#4-macos-桌面开发)。Windows 与 Linux 桌面版本尚未公开提供。

## 数据、安全与限制

- 项目、设置和日志默认保存在平台用户数据目录；可以显式指定其他用户数据根目录。
- API Key 只从环境变量或系统钥匙串读取。普通日志不保存完整 Prompt、源文或鉴权 Header；
  Debug 模式可能保存敏感请求内容。
- Web 默认只允许本机访问。局域网共享必须显式开启，当前使用 HTTP，不适合公网暴露。
- 基础安装支持 TXT、EPUB 和随应用资源提供的 SRT 插件。未安装 Adapter 的格式不会自动转换。
- 应用不提供自动翻译质量评分或质量保证，关键内容必须人工检查。

## 文档索引

- [用户指南](docs/USER_GUIDE.md)：Web、桌面、项目工作流、格式、数据安全和 CLI。
- [最小产品规范](docs/MINIMAL.md)：稳定产品语义、核心不变量和阶段边界。
- [Adapter 契约](docs/ADAPTERS.md)：LLM Adapter、Document Adapter、插件和 Preset 协议。
- [模块职责](docs/MODULES.md)：实现架构、模块 ownership 和依赖方向。
- [开发指南](docs/DEVELOPMENT.md)：环境、运行、调试、测试和打包。
- [产品路线图](docs/ROADMAP.md)：尚未实现的方向、进入条件和暂缓原因。

## 许可证

MIT
