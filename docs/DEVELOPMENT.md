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

桌面开发和打包面向 macOS 13 或更高版本的 arm64。先准备前端依赖并构建 Web；首次运行还需
按下一节构建 `build/managed-runtime-dist`，之后运行：

```bash
npm run build --prefix web
bash scripts/desktop-dev.sh
```

`desktop-dev.sh` 从 `build/managed-runtime-dist/bin/python3` 启动 Web 服务。
`ANOTHER_LLM_MANAGED_RUNTIME_DIR` 仅在 Tauri 开发构建中由脚本指向该目录；
`ANOTHER_LLM_WEB_PORT` 可更改监听端口，默认 `8765`。缺少 runtime 时脚本会停止并提示先构建。

原生文件、文件夹和导出位置选择由 Tauri command 提供。普通浏览器路径仍使用上传、服务端
目录浏览和下载，因此修改选择器时需要分别验证桌面与浏览器入口。

## 5. macOS 打包与内置 Python

打包包含 CPython 3.13.15 arm64，由 Python Build Standalone（PBS）带日期发布版 `20260901`
提供。唯一来源、资产 URL、SHA-256、目标与最低 macOS 版本记录在
[`pbs-macos-arm64.lock.json`](../packaging/managed-runtime/pbs-macos-arm64.lock.json)。下载前确认
该 lock 与官方 [PBS release 页面](https://github.com/astral-sh/python-build-standalone/releases/tag/20260901)
一致；不要自行替换资产或摘要。下载到本地后先校验官方锁定的 SHA-256，再解压到 staging：

```bash
ASSET="cpython-3.13.15+20260901-aarch64-apple-darwin-install_only_stripped.tar.gz"
mkdir -p build/managed-runtime-download build/managed-runtime-staging
curl -fL --output "build/managed-runtime-download/$ASSET" \
  "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%2B20260901-aarch64-apple-darwin-install_only_stripped.tar.gz"
shasum -a 256 "build/managed-runtime-download/$ASSET"
tar -xzf "build/managed-runtime-download/$ASSET" \
  --strip-components=1 -C build/managed-runtime-staging
```

`shasum` 输出必须与 lock 中的 `sha256` 完全一致；不一致时停止，不要解压或构建。构建时再次
校验资产名称、摘要和解释器目标。准备符合
[`requirements-macos-arm64.lock`](../packaging/managed-runtime/requirements-macos-arm64.lock)
哈希清单的离线 wheelhouse，路径为 `build/managed-runtime-wheelhouse`。在网络可用时，可用刚解压的
PBS Python 按该锁下载目标平台 wheel；组装和 Tauri 打包阶段本身不访问网络。完整构建会从当前
checkout 重建项目 wheel。运行：

```bash
mkdir -p build/managed-runtime-wheelhouse
build/managed-runtime-staging/bin/python3 -I -m pip download \
  --only-binary=:all: --require-hashes \
  --dest build/managed-runtime-wheelhouse \
  -r packaging/managed-runtime/requirements-macos-arm64.lock
```

```bash
bash scripts/build-app.sh
```

`build-app.sh` 需要 macOS arm64、Node.js/npm、Rust/Cargo/Tauri 工具链，以及带 setuptools 和
wheel 的 CPython 3.11+ 构建解释器。它使用 `ANOTHER_LLM_BUILD_PYTHON` 指定解释器；未设置时
使用 checkout 的 `.venv/bin/python`，两者都不可用时直接失败。它执行前端检查与构建、生成当前
项目 wheel、通过 hash-locked 离线 wheelhouse 组装 managed runtime，然后构建 Tauri app；构建阶段
不会下载 Python 资产或依赖。需要单独组装 runtime 时，先生成当前项目 wheel：

```bash
mkdir -p build/project-wheel
python -m pip wheel --no-deps --no-build-isolation \
  --wheel-dir build/project-wheel .
```

然后通过本地 lock、runtime、wheelhouse 和 wheel 组装：

```bash
bash scripts/build_managed_runtime_macos.sh \
  --runtime-source build/managed-runtime-staging \
  --pbs-lock packaging/managed-runtime/pbs-macos-arm64.lock.json \
  --pbs-archive build/managed-runtime-download/cpython-3.13.15+20260901-aarch64-apple-darwin-install_only_stripped.tar.gz \
  --project-wheel "$(find build/project-wheel -maxdepth 1 -type f -name '*.whl' -print -quit)" \
  --wheelhouse build/managed-runtime-wheelhouse \
  --requirements-lock packaging/managed-runtime/requirements-macos-arm64.lock \
  --output build/managed-runtime-dist
```

该脚本将 PBS lock 一并放入 runtime 作为来源记录；只从本地 wheelhouse 安装，不使用 pip resolver
或网络回退。缺少输入、摘要或 wheel 不匹配时会失败并显示原因。

完整构建输出是 macOS arm64 `.app` 和 zip；当前本地构建使用 ad hoc 签名，不是 Developer ID
签名或公证发行件：

```text
dist/another-llm-translator-<版本>-macos-arm64/
```

以下目录都是构建产物，不应提交：

```text
app/web_dist/
build/managed-runtime-download/
build/managed-runtime-staging/
build/managed-runtime-wheelhouse/
build/managed-runtime-dist/
src-tauri/target/
dist/
```

探针 PBS 源归档及可移动性时使用真实 lock 与下载资产：

```bash
python scripts/probe_managed_runtime_macos.py \
  --lock packaging/managed-runtime/pbs-macos-arm64.lock.json \
  --archive "build/managed-runtime-download/$ASSET"
```

成功时退出码为 `0`、JSON `status` 为 `"ok"`，包含摘要、macOS 部署目标、许可文件和 relocation
结果；失败时退出码非零并输出 `status: error`。当前 PBS 二进制的 Mach-O 部署目标检查通过。

### 插件依赖运行时探针（受控实验）

以下两个脚本只针对明确的 wheel、解释器或 `.app` 做可复现的分发形态验证，不定义公共插件依赖
协议。

#### Web wheel/clean venv

前置条件是已按上面的流程准备 Python 3.11+ 开发环境，并能构建 wheel。命令会创建临时 venv，
安装给定 wheel，清除 `PYTHONPATH` 与 `VIRTUAL_ENV` 后验证官方插件导入，再启动 wheel 的 Web
entrypoint，发现受控外部 Adapter 并导入两个 Segment：

```bash
WHEEL_DIR="$(mktemp -d)"
python -m pip wheel --no-deps . --wheel-dir "$WHEEL_DIR"
python scripts/probe_plugin_dependencies.py \
  --wheel "$WHEEL_DIR"/*.whl \
  --python "$(command -v python)" \
  --import-target plugins.srt.plugin
```

脚本在标准输出打印一个 JSON 对象；成功时退出码为 `0` 且 `status` 为 `"ok"`，并应看到
`dependency_imported`、`adapter_discovered`、`file_imported`、`port_released` 和
`temporary_root_removed` 均为 `true`。临时 venv 和 fixture 会由脚本清理。此处的 pip 安装只
是探针实现的一部分，不是应用的自动安装或 resolver 契约。

失败时退出码非零，JSON 中的 `status` 为 `"error"`，`code` 和 `message` 给出定位信息；修正
wheel、解释器或构建环境后重跑，不应把失败当作静默回落。常见输入/导入失败包括
`wheel_missing`、`interpreter_missing`、`wheel_install` 和 `import_failed`。

#### Packaged macOS

前置条件是已完成 macOS arm64 打包并得到 `.app`：

```bash
bash scripts/build-app.sh
APP="dist/another-llm-translator-<版本>-macos-arm64/Another LLM Translator.app"
python3 scripts/probe_packaged_plugin_dependencies.py "$APP"
```

直接探针验证 `.app` 中的 managed Python、二进制 wheel 与官方插件资源，并在清除宿主 Python 路径后
运行 Web 和外部插件 smoke。成功时退出码为 `0`、JSON `status` 为 `"ok"`；失败时退出码非零，
JSON 的 `code` 和 `message` 指出 bundle、依赖、插件或清理问题。探针不会改用宿主解释器。

### packaged app 的外部插件 smoke

先完成完整打包，再把生成的 `.app` 传给运行时 smoke 脚本：

```bash
bash scripts/build-app.sh
bash scripts/verify-external-plugin-runtime-macos.sh \
  dist/another-llm-translator-<版本>-macos-arm64/Another\ LLM\ Translator.app
```

脚本使用临时用户数据根启动同一个 packaged app，必须依次通过三项检查：首次启动未加载
临时插件；重启后发现外部 Document Adapter；通过项目创建、概览和 Segment 查询确认该
Adapter 导入两个源文 Segment。插件字段和运行时边界见 [Adapter 契约](ADAPTERS.md)。

该 smoke 仅适用于 macOS arm64 打包产物；本次构建和 GUI smoke 未在 macOS 13 实机执行。若
checkout 位于外部 APFS 卷且 app 启动停在 Python 初始化，可将 `.app` 复制到本机 APFS 卷后重试。

## 6. 调试与诊断

普通日志用于查看启动、请求摘要、重试和失败原因。Debug 模式会额外保存完整请求、响应和
执行诊断，可能包含 Prompt、源文或模型输出；只能在明确的本地诊断场景启用，完成后应关闭，
不得提交生成的数据。

排查顺序建议保持聚焦：

1. 先用对应 CLI 子命令的 `--help` 和最小可复现项目确认入口参数。
2. 查看终端错误和项目日志中的明确失败原因。
3. Web 问题同时检查浏览器控制台与后端日志。
4. 桌面问题再检查 bundled Python 启动、端口和 Tauri 日志。
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
