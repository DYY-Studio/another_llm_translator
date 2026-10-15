# Another LLM Translator

面向本机和可信局域网，支持中断恢复的 LLM 文档翻译工作台。

> [!WARNING]
> **Vibe Coding**
>
> 本项目完全使用 AI 协作开发
>
> **快速演进**
>
> 本项目仍处于功能快速演进阶段，无法保证端点和契约的长期稳定性

## 功能

- 导入 TXT、EPUB 和插件支持的文档，并按原文件分别管理和导出
- 扫描、编辑、交换和发布术语
- 使用术语自动决策自动完成术语库整理
- 分阶段执行翻译、校对和润色
- 多阶段自动连续运行
- 取消、失败或重启后可继续未完成内容
- 内置支持 OpenAI-compatible、OpenAI Responses、Gemini 和 Anthropic 请求响应格式，并允许用户添加自有格式
- 通过官方 OAuth 使用自己的 ChatGPT Plan 额度
- 为不同阶段选择不同的 LLM 提供商、模型和参数 (LLM Preset)
- 在 Web 中管理项目、Prompt、术语、译文、诊断并导出和传输成品文件
- Web UI 具有完整移动端支持

## 特色
### 术语与实体知识库（术语库）
- 一个术语多个别名，不需要反复建立多条目
- 将昵称、缩写、外号等同属一实体的术语集中为一个术语组
- 全术语组共用一组实体说明
- 实体说明可溯源，可向 LLM 展示信息出现的相对位置
### 术语自动决策
由 LLM 自动完成：
- 术语分组
- 类别、译名选取和统一
- 聚合实体说明

彻底解放繁琐的术语整理任务

## 开始使用

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

1. 把 API Key 放在环境变量并重启 或 在`设置 → 凭据`写入到系统钥匙串
2. 在`设置`中选择或编辑 LLM Preset，选择 Adapter，并填写 Base URL、模型和凭据引用
3. 确认新项目的默认目标语言，新建项目并添加文档
4. 按需执行`术语 → 术语自动决策 → 翻译 → 校对 → 应用校对 → 润色 → 应用润色`
5. 在`导出`中选择结果阶段、文件范围、格式和单语/双语输出，然后导出

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

仓库包含 Tauri 桌面壳，在应用窗口中运行同一套本地 Web 工作台，并提供原生文件和导出位置选择。

当前该方面并不是开发重心，尚无Release计划，需要从源码构建。

开发运行和打包步骤见[开发指南](docs/DEVELOPMENT.md#4-macos-桌面开发)。

Windows 与 Linux 桌面版本暂无推进打算。

## 数据、安全与限制

- 项目、设置和日志默认保存在平台用户数据目录，你也可以显式指定其他用户数据根目录。
- API Key 只从环境变量或系统钥匙串读取。
- Debug 模式可能保存敏感请求内容。
- Web 默认只允许本机访问，局域网共享必须显式开启。
- 基础安装支持 TXT、EPUB 和随应用资源提供的 SRT 插件。
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
