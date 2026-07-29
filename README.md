# Minimal LLM Translator

面向多个 TXT 文件的最小工程化翻译验证工具。实现以 Segment 为恢复和进度单位，
支持术语、翻译、校对建议、润色建议、应用建议及单语/双语导出。

## 安装

需要 Python 3.11 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip check
```

先编辑全局模板 `config/config.toml` 和 `prompts/*.middle.txt`。将配置中
`llm.api_key_env` 指定的密钥放入环境变量：

```bash
export LLM_API_KEY="..."
```

## 基本流程

```bash
python -m app.main init novel.txt --name novel
python -m app.main run-all novel
python -m app.main inspect novel
python -m app.main apply novel --stage proofreading --all
python -m app.main export novel --stage proofread --bilingual
```

项目默认保存在 `projects/<name>/`。`run-all` 只生成校对和润色建议，不会自动
应用。可先为任一阶段加入 `--dry-run` 查看范围、Chunk 数和 Token 估算。

完整行为和 MVP 边界见 [`docs/MINIMAL.md`](docs/MINIMAL.md)，贡献与分支流程见
[`AGENTS.md`](AGENTS.md)。

## 开发期结果编辑器

需要人工检查或修正测试项目结果时，可启动独立的本地编辑器：

```bash
python -m app.editor novel
```

编辑器只绑定 `127.0.0.1`，直接编辑术语、翻译、校对和润色结果，不调用 LLM
或主 CLI 流程。使用期间不要同时运行会写入同一项目的 CLI 命令。它是开发辅助
工具，不用于修改源 Segment、配置或 Prompt。

## 验证

```bash
python -m pytest -q
python -m app.main --help
python -m app.editor --help
```

测试使用模拟 HTTP 响应，不会调用真实模型。
