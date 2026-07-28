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

## 验证

```bash
python -m pytest -q
python -m app.main --help
```

测试使用模拟 HTTP 响应，不会调用真实模型。
