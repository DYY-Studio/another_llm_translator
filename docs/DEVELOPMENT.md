# 开发指南

本文只负责 Another LLM Translator 的开发环境、运行、调试、测试和打包。仓库级工程原则、
测试取舍、兼容策略和提交要求以根目录的 [`AGENTS.md`](../AGENTS.md) 为准；代码模块与依赖
方向见[模块职责](MODULES.md)。产品行为和协议分别见[最小产品规范](MINIMAL.md)与
[Adapter 契约](ADAPTERS.md)。

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

`requirements.txt` 只包含运行时依赖；`requirements-dev.txt` 增加测试和构建依赖。仓库内的
示例插件通过 `plugins/` 目录直接参与源码运行，不单独安装。

开发用 API Key 只能通过 Preset 引用的环境变量或系统钥匙串提供，不得写入仓库文件。

## 2. 后端与 CLI 开发

查看入口帮助：

```bash
python -m app.main --help
python -m app.web --help
```

启动本地 Web 后端：

```bash
python -m app.web
```

默认地址为 `http://127.0.0.1:8765`，可通过 `--port` 更换端口。开发时可以设置
`ANOTHER_LLM_USER_ROOT` 指向专用测试数据目录，避免混用日常项目。

后端模块的职责和入口依赖见[模块职责](MODULES.md)。调试业务行为时应从 CLI/Web 入口追踪到
共享领域模块，不在前端复制项目状态或恢复逻辑。

## 3. Web 开发

安装依赖并执行静态检查与生产构建：

```bash
npm ci --prefix web
npm run typecheck --prefix web
npm run build --prefix web
```

生产构建写入 `app/web_dist/`。完成受影响的前端变更后，还应启动实际 Web 界面，通过浏览器
验证交互和视觉；需要时保存截图作为审核依据。

`app/web_dist/` 是构建产物，不应提交。

## 4. macOS 桌面开发

先安装 Python 与前端依赖并构建 Web，然后运行：

```bash
npm run build --prefix web
bash scripts/desktop-dev.sh
```

开发脚本使用以下环境变量：

- `ANOTHER_LLM_PYTHON`：开发模式使用的 Python，默认 `.venv/bin/python`。
- `ANOTHER_LLM_REPO_ROOT`：桌面壳使用的仓库根目录，脚本会自动设置。
- `ANOTHER_LLM_WEB_PORT`：sidecar Web 端口，默认 `8765`。

桌面壳会启动本地 Web sidecar，再在 Tauri 窗口中加载它。开发时若端口已被残留进程占用，
先确认进程来源并结束残留实例，再重新启动；不要假定正在监听的服务就是本次构建。

原生文件、文件夹和导出位置选择由 Tauri command 提供。普通浏览器路径仍使用上传、服务端
目录浏览和下载，因此修改选择器时需要分别验证桌面与浏览器入口。

## 5. macOS 打包

完整构建：

```bash
bash scripts/build-app.sh
```

脚本依次执行前端类型检查与构建、PyInstaller sidecar 冻结和 Tauri 打包。sidecar 构建会收集
构建环境中的官方 `plugins/` 目录资源；用户插件在运行时从用户数据目录读取。

当前输出是面向 macOS arm64 的未签名 ad hoc `.app` 和 zip：

```text
dist/another-llm-translator-<版本>-macos-arm64/
```

签名、公证和公开发行流程尚未建立。未签名应用首次打开可能需要通过“系统设置 → 隐私与
安全性”或右键“打开”放行。

以下目录都是构建产物，不应提交：

```text
app/web_dist/
sidecar-dist/
src-tauri/target/
dist/
```

标准 Python wheel 也会把官方目录插件的运行时代码和 manifest 安装到内置资源目录；构建
wheel 时使用主虚拟环境执行 `python -m pip wheel --no-deps . --wheel-dir <输出目录>`。
sidecar 与 wheel 都不依赖插件 entry point 安装，用户插件仍从用户数据根的 `plugins/` 目录
读取。

## 6. 调试与诊断

普通日志用于查看启动、请求摘要、重试和失败原因。Debug 模式会额外保存完整请求、响应和
执行诊断，可能包含 Prompt、源文或模型输出；只能在明确的本地诊断场景启用，完成后应关闭，
不得提交生成的数据。

排查顺序建议保持聚焦：

1. 先用对应 CLI 子命令的 `--help` 和最小可复现项目确认入口参数。
2. 查看终端错误和项目日志中的明确失败原因。
3. Web 问题同时检查浏览器控制台与后端日志。
4. 桌面问题再检查 sidecar 启动、端口和 Tauri 日志。
5. 只有普通诊断不足时才启用 Debug，并使用不含敏感内容的样本。

测试和诊断不得调用真实模型；使用确定性的模拟响应。

## 7. 验证

根据修改范围执行最小充分验证：

```bash
python -m pip check
python -m pytest -q
python -m app.main --help
python -m app.web --help
npm run typecheck --prefix web
npm run build --prefix web
git diff --check
```

- 后端行为变更：运行相关测试，合并前优先运行完整 Python 测试。
- Web 变更：运行 TypeScript 检查和前端构建，并在浏览器中验证受影响交互。
- Adapter、存储、恢复或协议变更：运行对应契约和回归测试。
- 纯文档变更：检查链接、命令、标题层级和 `git diff --check`，无需运行应用测试。

更细的测试取舍、减法审查和提交规则不在本文重复，统一遵循 [`AGENTS.md`](../AGENTS.md)。
