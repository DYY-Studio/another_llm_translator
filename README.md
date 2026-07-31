# Minimal LLM Translator

本地工程化翻译工具。实现以 Segment 为恢复和进度单位，支持 TXT/EPUB、
声明式 LLM Adapter、术语、翻译、校对建议、润色建议、应用建议及单语/双语
文档导出。

## 安装

需要 Python 3.11 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip check
```

先编辑全局模板 `config/config.toml`、`prompts/*.middle.txt` 和选中的
`llm_presets/<preset_id>.json`。将 Preset 中 `api_key_env` 指定的密钥放入
环境变量。项目、全局模板和 Run 续作只接受 `llm.preset`，不支持内联连接
配置：

```bash
export LLM_API_KEY="..."
```

如需为 LLM 请求使用显式 HTTP/HTTPS 代理，在 Preset 中设置：

```json
{
  "proxy_url": "http://127.0.0.1:7890"
}
```

留空时仍允许 HTTPX 使用标准代理环境变量。

## 基本流程

```bash
python -m app.main init novel.txt --name novel
python -m app.main run-all novel
python -m app.main inspect novel
python -m app.main apply novel --stage proofreading --all
python -m app.main export novel --stage proofread --bilingual
python -m app.main terms-export novel glossary.csv
```

项目默认保存在 `projects/<name>/`。`run-all` 只生成校对和润色建议，不会自动
应用。可先为任一阶段加入 `--dry-run` 查看范围、Chunk 数和 Token 估算。

也可先建立不预设格式的空项目，再混合追加或移除项目源文件：

```bash
python -m app.main init --empty --name novel
python -m app.main files-add novel chapter-1.txt chapter-2.txt
python -m app.main files-add novel appendix.epub
python -m app.main files-remove novel F0001
python -m app.main export novel --stage translated --file F0002
```

每个 File 保存自己的 TXT/EPUB 来源 Adapter；原格式导出逐 File 重建，也可用
`export --format txt` 统一输出 TXT。移除文件不会清理历史阶段记录和既有输出，
但被移除 Segment 不再参与统计、复用和导出。

独立运行 `terminology`、`translate`、`proofread` 或 `polish` 时，如发现同阶段
未完成 Run，交互终端会询问继续还是新建。脚本或 CI 中需明确使用
`--resume-run` 或 `--decline-run`；续作沿用旧范围，但使用当前项目配置和
Prompt。

术语表可通过 `terms-import` 和 `terms-export` 在 JSON/CSV 间交换。重新执行
`terminology --force` 会把新扫描结果合并到当前术语库；未再次发现的术语不会
自动删除，人工移除决定持续生效。

EPUB 项目使用：

```bash
python -m app.main init book.epub --name book --document-adapter epub
python -m app.main export book --stage translated --bilingual
```

完整行为见 [`docs/MINIMAL.md`](docs/MINIMAL.md)，后续阶段见
[`docs/ROADMAP.md`](docs/ROADMAP.md)，扩展边界见
[`docs/ADAPTERS.md`](docs/ADAPTERS.md)，贡献与分支流程见
[`AGENTS.md`](AGENTS.md)。

当前产品路线已完成 Stage 6（按文件筛选导出）；下一阶段是四阶段独立 LLM
Preset。未来行为以路线图为准，不属于当前 MVP 契约。

## 本地 Web Alpha

构建前端后启动只监听回环地址的完整工作台：

```bash
cd web
npm ci
npm run build
cd ..
python -m app.web
```

打开 `http://127.0.0.1:8765`。Web 与 CLI 共用同一项目目录、阶段执行、Run、
限速与恢复逻辑，不建立第二套数据库。可创建空的或带文件的 TXT/EPUB 项目，
并在概览追加或批量移除 TXT/EPUB 源文件；项目配置通过覆盖全部现有字段的分组
表单编辑，Prompt 和 JSON LLM Adapter 保持独立高级编辑器。设置页还可在未
打开项目时管理全局配置、全局 Prompt 和实时 LLM Preset；Preset 支持经过
冲突检查的自定义附加 JSON body 和脱敏请求预览。也可运行或取消
阶段任务，并审校、apply 与导出。术语工作区
支持搜索、冲突裁决、JSON/CSV 导入导出、新增、编辑、移除和恢复；术语与
Segment 列表支持 Ctrl/Cmd/Shift 经典多选。翻译、校对和润色可按所选或当前
过滤范围清除结果，校对和润色还可批量应用建议。顶栏外观按钮可在跟随系统、
浅色和深色之间切换，选择只保存在当前浏览器。启动术语、翻译、校对或润色前
会显示运行选项，明确选择未完成 Run 的续用/新建，以及已有结果的复用/强制
重做。

## 开发期结果编辑器

需要人工检查或修正测试项目结果时，可启动独立的本地编辑器：

```bash
python -m app.editor novel
```

编辑器只绑定 `127.0.0.1`，可裁决术语冲突、移除或恢复误提术语，并通过原文
与译文、基准与建议对照直接检查和编辑翻译、校对与润色结果。三个文本阶段可按
当前状态快速过滤，右侧同时显示同文件的邻近上下文；它不调用 LLM 或主 CLI
流程。使用期间不要同时运行会
写入同一项目的 CLI 命令。它是开发辅助工具，不用于修改源 Segment、配置或
Prompt。

## 验证

```bash
python -m pytest -q
python -m app.main --help
python -m app.editor --help
python -m app.web --help
cd web && npm run typecheck && npm run build
```

测试使用模拟 HTTP 响应，不会调用真实模型。
