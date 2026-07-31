# 基于 LLM 的工程化翻译器

## 最小可行性验证实现文档

版本：MVP 0.3

实现语言：Python 3.11+

运行方式：单机、本地 CLI/Web、异步并发

存储方式：项目文件夹、JSON、JSONL、TOML、TXT、EPUB

LLM 接口：声明式非流式 JSON POST Adapter

---

# 1. 目标与边界

## 1.1 验证目标

本项目只验证下面这条工程化翻译链路是否可行：

```text
TXT/EPUB 导入
→ Segment 化
→ 术语提取
→ 翻译
→ 校对建议
→ 可选应用校对
→ 润色建议
→ 可选应用润色
→ 原文档格式导出
```

MVP 需要回答：

1. TXT 与 EPUB 能否稳定导入、处理并按原格式导出。
2. 文档的可翻译单元能否稳定映射为 Segment，TXT 空行是否保持可见结构。
3. 长文本能否在模型 Token 限制内动态分块。
4. 术语能否从项目文本提取、合并并注入相关翻译请求。
5. 翻译、校对和润色结果能否准确对应 Segment。
6. 上文能否帮助模型判断人物、指代、语气和语义。
7. 并发、RPM、ITPM 和有限重试是否有效。
8. 中断后能否只继续未完成 Segment。
9. 修改 Chunk、限流或调度参数后能否继续复用已完成结果。
10. 单语和双语文档是否可读，并保持格式对应的结构与资源。

术语召回率约 85%、核心术语一致率约 90% 只是在固定样本、模型、Prompt 和配置下的观测参考，不作为随机自动化硬门槛。

## 1.2 核心不变量

### File 是内容边界

任何 LLM 请求、Chunk 和参考上文都不得跨文件。术语库可以在项目级汇总，但单次术语请求仍不得跨文件。

### Segment 是进度单位

Segment 是 Document Adapter 返回的有序可翻译单元：TXT 中对应逻辑行，EPUB
中对应 XHTML 文本槽。术语扫描、翻译、校对和润色的完成结果、失败记录和恢复
判断全部绑定 `segment_id`。

非空 Segment 的连续前导 Unicode 空白以源文为准。模型仍接收完整文本，但翻译、
校对建议、润色建议、apply 和导出在本地移除模型生成的前导空白，并恢复源
Segment 的精确前缀；正文内部和尾随空白不做本地保护。该行为不依赖 Prompt。

空 Segment：

- 源文为空字符串或只包含 Unicode 空白字符，例如普通空格、Tab 或全角空格。
- 保留在源数据中。
- 不提交 LLM。
- 不要求写阶段结果。
- 导出时统一恢复为普通空行，不保留原始空白字符。

### Chunk 是临时请求包装

Chunk 由当前 Run 动态生成，只能包含：

- 同一个 `file_id`。
- 按 `line_index` 保持顺序的非空 Segment。
- 当前命令选定范围内仍需处理的 Segment。

两个待处理非空 Segment 之间只有一个或多个空 Segment 时可以进入同一 Chunk；空 Segment 本身仍不提交 LLM。已完成、筛选范围外或其他不在待处理集合中的非空 Segment 会结束当前 Chunk。部分响应、格式修正和翻译校验修复也必须使用相同规则重新分组。

Chunk 没有长期业务身份，不能用于判断进度或恢复。只有启用调试模式时才持久化 Chunk Manifest。

### Run 是一次执行记录

除 `--dry-run` 外，术语、翻译、校对、润色和 apply 通常创建 Run。四个独立
LLM 阶段命令也可以续用同阶段最近的未完成 Run；`run-all` 不参与续作。

Run 保存：

- 阶段和选定范围。
- 当前 `stage_fingerprint`。
- 项目配置快照。
- LLM 阶段实际使用的完整 Prompt。
- 开始、结束和结果摘要。

apply 不调用 LLM，因此只保存配置和输入结果引用，不保存虚构 Prompt。

### 设置变化由用户决断

设置变化不能自动清空、隐藏或标记旧进度失效；是否复用必须由用户决定。

- 已有 completed 继续作为可用结果。
- 尚未完成的 Segment 使用当前设置处理。
- 同一阶段允许出现来自不同设置指纹的结果。
- `inspect` 和阶段命令必须报告混合设置。
- 用户只有在明确使用 `--force` 时才重做选定范围。

## 1.3 非目标

MVP 不实现：

- 远程、多用户或公网 Web 服务。
- 数据库、消息队列或分布式执行。
- 同一项目的多个写任务并发运行或并发写入合并。
- 网络共享盘协调。
- EPUB 以外的复杂文档格式。
- Python LLM Adapter、自动 Provider 判断或通用工作流引擎。
- 远程插件、自动安装、插件市场或插件沙箱。
- Repository、Service、依赖注入或迁移框架。
- 通用术语编辑器或复杂自动冲突裁决。
- 自动翻译质量评分。
- 源 Segment 的增量编辑。
- TXT 字节级往返、原编码复刻或换行符保真。
- 面向未来版本的完整数据迁移能力。

---

# 2. 项目、输入与配置

## 2.1 最小技术栈与职责

运行时第三方依赖：

```text
httpx
chardet
fastapi
uvicorn
python-multipart
```

React/Vite/TypeScript 只用于构建随 Python 包分发的 Web 静态资源。标准库负责
CLI、异步调度、JSON/JSONL/TOML、Hash、日志、路径、原子替换、插件发现、
EPUB ZIP/XML 处理和 Unicode 归一化。

代码只需覆盖以下职责，不预先规定模块数量：

- CLI 和配置加载。
- 项目初始化及模板同步。
- TXT/EPUB Document Adapter 与 Segment 化。
- JSON/JSONL 持久化。
- Prompt 渲染、Token 估算和 Chunk 生成。
- 声明式 LLM Adapter、HTTP 调用、限流和重试。
- 术语、翻译、校对、润色和 apply。
- inspect 与原文档格式导出。
- 只绑定本机回环地址的 Web Alpha。

## 2.2 项目内容

项目至少包含：

```text
project.json
config.toml
prompts/
llm_adapters/
input/
source/files.jsonl
source/segments.jsonl
source/adapters/{document_adapter_id}/state.json
terminology/terms.json
terminology/overrides.json
terminology/active_task.json
terminology/scans.jsonl
terminology/candidates.jsonl
stages/*.jsonl
runs/{run_id}/manifest.json
logs/app.log
output/
```

Run 的配置和 Prompt 快照放在对应 Run 目录。启用调试模式时，该目录额外保存 Chunk、Attempt 和 Payload。

全局模板位于应用目录：

```text
config/config.toml
prompts/terminology.middle.txt
prompts/translation.middle.txt
prompts/proofreading.middle.txt
prompts/polishing.middle.txt
llm_adapters/openai-compatible.json
```

## 2.3 初始化与文件发现

TXT 支持目录或显式文件；EPUB 每个项目接受一个显式文件：

```bash
python -m app.main init INPUT... --name PROJECT_NAME
python -m app.main init INPUT_DIR --recursive --name PROJECT_NAME
python -m app.main init BOOK.epub --document-adapter epub --name PROJECT_NAME
```

规则：

- 显式文件按参数顺序处理。
- 目录按相对路径进行确定性的简单自然排序。
- TXT 未传 `--recursive` 时只读取目录第一层。
- 递归发现时忽略符号链接。
- 显式符号链接输入直接拒绝。
- 多个输入映射到同一导出相对路径时拒绝初始化。
- 目录输入保存相对目录树；显式文件使用 basename。
- 输入文件复制到项目 `input/`，后续阶段只读取项目数据。
- 项目记录 `document_adapter_id`、版本和不透明状态位置；旧 TXT 项目缺少这些
  字段时按内置 `txt` 解释。

EPUB Adapter 按 OPF spine 顺序读取 XHTML 中可见的非空 `text` 和 `tail`
槽位。原 EPUB 及定位状态用于重建输出；导航、元数据、图片、CSS、字体和其他
未翻译资源保持原样。ZIP 路径穿越、符号链接、重复路径、异常条目数/解压大小/
压缩比、越界资源以及 XML DTD/实体声明会被拒绝。

项目创建后，`source/segments.jsonl` 是源内容真相。手工修改项目 `input/` 或 `segments.jsonl` 均不受支持；需要修改源文时重新创建项目。

## 2.4 输入编码与 Segment 化

解码规则：

1. 存在 UTF-8、UTF-16 或 UTF-32 BOM 时优先采用对应编码。
2. 否则使用 `chardet`。
3. `GB2312` 和 `GBK` 统一按 `GB18030` 严格解码，`ASCII` 按 UTF-8 解码。
4. 首选编码失败时只尝试一次配置的 fallback。
5. fallback 仍失败时拒绝该文件，不使用 replacement decode。

解码后统一内部行分隔，再切分：

```python
text = text.replace("\r\n", "\n").replace("\r", "\n")
segments = text.split("\n")
```

这样可以保留文件首部、正文中的连续空行和尾部空 Segment。原换行符、BOM、末尾换行状态和输入字节不进入项目契约，也不决定输出格式。

每个文件记录：

- `file_id`
- `file_order`
- `original_name`
- `stored_name`
- `encoding_detected`
- `encoding_used`
- `segment_count`

每个 Segment 记录：

- `segment_id`
- `file_id`
- `line_index`
- `source`
- `is_empty`

ID 示例：

```text
F0001
F0001-S000001
F0001-S000002
```

## 2.5 项目配置

配置严格拒绝未知键。

```toml
[project]
target_language = "简体中文"
output_encoding = "utf-8-sig"

[input]
encoding_confidence_threshold = 0.60
fallback_encoding = "utf-8"

[llm]
adapter = "openai-compatible"
base_url = "https://example.com/v1"
endpoint = "/chat/completions"
model = "example-model"
api_key_env = "LLM_API_KEY"
proxy_url = ""

temperature_terminology = 0.1
temperature_translation = 0.2
temperature_proofreading = 0.1
temperature_polishing = 0.3

max_output_tokens = 4096
context_window_tokens = 16384
context_safety_margin_tokens = 512

[execution]
max_parallel = 4
requests_per_minute = 30
input_tokens_per_minute = 50000
request_timeout_seconds = 120
scheduling_mode = "ordered_by_file"
token_safety_factor = 1.25

[chunking]
target_chunk_input_tokens = 11000
allow_split_oversized_segment = true

[context.translation]
enabled = true
previous_segments = 3

[context.proofreading]
enabled = true
previous_segments = 3

[context.polishing]
enabled = true
previous_segments = 3

[context.terminology]
enabled = true
previous_segments = 3

[terminology]
unicode_normalization = "NFKC"
case_insensitive = true
max_terms_per_segment = 100
alias_primary_collision = "conflict"

[validation.translation]
japanese_kana = false
korean_hangul = false
max_retry_attempts = 2
exhausted_mode = "fail"

[retry]
http_max_attempts = 6
format_max_attempts = 2
base_delay_seconds = 2.0
max_delay_seconds = 60.0
jitter_seconds = 1.0

[debug]
enabled = false
inject_429_every = 0
inject_500_every = 0
inject_timeout_every = 0
inject_invalid_json_every = 0
inject_missing_segment_every = 0
```

`unicode_normalization` 和 `case_insensitive` 当前尚未实现为可变行为，只是后续
能力占位。术语实现固定使用 `NFKC` 和 `casefold`，因此现阶段只接受上述值。
待核心主流程完成全面验证后，再基于真实用例实现其他取值并补充测试。

`requests_per_minute = 0` 和 `input_tokens_per_minute = 0` 分别表示禁用 RPM
和 ITPM 限速。两者可以独立禁用；ITPM 为 0 时也不参与 Chunk 目标及单请求
Token 上限判断。模型上下文窗口和 `max_parallel` 始终生效。

API Key 只从环境变量读取，不写入项目、Run、日志或 Payload。

`llm.adapter` 选择项目 `llm_adapters/<id>.json`。定义是完整的 Header/body
模板和成功响应 JSON Pointer；可加入 Provider 自定义 JSON 字段。定义 Hash
进入阶段指纹，定义副本进入 Run 快照，解析后的密钥不进入任何持久化内容。完整
schema 见 `docs/ADAPTERS.md`。

`proxy_url` 为空时不显式设置代理，HTTPX 仍按默认行为读取 `HTTP_PROXY`、
`HTTPS_PROXY` 和 `NO_PROXY`。非空值只接受 `http://` 或 `https://` URL；
MVP 不增加 SOCKS 依赖。代理属于传输设置，不进入阶段指纹。

允许的调度模式：

```text
ordered_by_file
parallel
```

允许的翻译校验耗尽策略：

```text
fail
warning
```

## 2.6 全局模板同步

`init` 将全局配置和四个提示词完整复制为项目工作副本。项目执行始终使用项目副本。

项目只记录：

```text
global_bundle_hash_seen
```

Bundle Hash 对全局配置和四个提示词的相对路径及内容计算。它只用于发现新的全局模板，不参与阶段结果判断。

项目命令执行前：

1. 全局 Bundle 与 `global_bundle_hash_seen` 相同：直接继续。
2. 出现新 Bundle 且为交互终端：展示变更摘要，询问更新或保留。
3. 选择更新：先备份当前项目配置和 prompts 到 `snapshots/template_updates/{timestamp}/`，再复制全局 Bundle，并记录新 Hash。
4. 选择保留：不修改项目副本，只记录新 Hash，避免重复询问。
5. 非交互环境：警告并保留项目副本，不更新 seen Hash。
6. 全局模板缺失或无效：不得覆盖有效项目副本。

`--dry-run` 只报告存在模板差异，不询问、不更新也不备份。

---

# 3. 数据、进度与结果选择

## 3.1 schema v1 公共头部

内部 JSON 和 JSONL 记录使用 `schema_version = 1`。公共字段在适用时包含：

```text
schema_version
record_type
record_id
project_id
stage
segment_id
status
run_id
request_id
parent_record_id
created_at
```

阶段名、记录状态、错误类别、校对状态和 validation 状态使用集中定义的稳定字符串枚举。

读取器：

- 拒绝不支持的主版本。
- 允许 schema v1 出现未知的可选记录字段。
- 不要求外部 LLM wire payload 添加内部公共头部。

## 3.2 用户可见的三种进度状态

阶段进度只使用：

```text
pending
completed
failed
```

含义：

- `completed`：该 Segment 至少存在一条成功结果。
- `failed`：不存在成功结果，且最近一次尝试失败。
- `pending`：不存在成功或失败结果。

持久化阶段结果只写：

```text
completed
failed
```

`pending` 由缺少记录推导。缺少上游结果是阶段启动检查问题，不建立 `blocked` 状态。设置变化只产生 warning，不建立 `stale` 状态。

结果选择始终查找最新 completed，而不是机械采用最后一条 JSONL 记录。同一 Segment 强制重做失败时：

- 旧 completed 继续作为当前可用结果。
- 最近失败仍可由 inspect 查看。
- 当前命令返回“选定范围未全部完成”。

普通阶段命令只处理没有 completed 的 pending 和 failed。`--force` 才把选定范围内已有 completed 的非空 Segment 重新加入待处理集合。

## 3.3 阶段设置指纹

`stage_fingerprint` 是唯一用于设置差异提示的阶段 Hash。

它对规范 JSON 计算 SHA-256。规范数据包含：

- stage
- target language
- model
- 实际完整 Prompt 文本
- 当前阶段 temperature
- 当前阶段 context 配置
- `scheduling_mode`
- 影响阶段语义的配置
- 使用的 `terms_revision`，适用时
- LLM Adapter ID 与定义内容 Hash

翻译还包含启用的文字校验器及 `exhausted_mode`。最大校验重试次数只影响执行，不进入指纹。

上述模型、Prompt、temperature、context、调度和术语字段适用于 LLM 阶段。apply 的指纹只包含 apply 阶段、应用规则版本、建议类型和是否允许旧基准，不虚构模型或 Prompt 字段。

指纹不包含：

- Chunk ID、大小或组合
- Token 目标与模型窗口配置
- 并发、RPM、ITPM
- timeout
- HTTP、格式或校验重试次数
- Run ID

每条 completed/failed 结果记录本次 Run 的 `stage_fingerprint`。命令开始时：

1. 计算当前阶段指纹。
2. 汇总选定范围内已有 completed 所使用的指纹。
3. 不同且未使用 `--force` 时，必须由交互用户确认复用，或在非交互环境
   显式指定 `--reuse-mixed-fingerprints`。
4. 用户拒绝复用时停止且不创建本阶段 Run；使用 `--force` 才重做选定范围。
5. 新 pending 使用当前设置。
6. `inspect` 报告每个阶段使用过的指纹数量和当前指纹覆盖数量。

`--resume-run` 已经显式授权按当前设置续用旧 Run，不重复询问设置指纹。
`--dry-run` 不询问，只报告正式执行所需的选择。

不再计算 Request、Response、Prompt、Config、Source、Chunk Manifest、逐 Segment 术语或术语内容 Hash。

## 3.4 Run

Run Manifest 最少包含：

```json
{
  "schema_version": 1,
  "record_type": "run",
  "record_id": "RUN-20260729-120000-TR",
  "project_id": "PRJ-...",
  "run_id": "RUN-20260729-120000-TR",
  "stage": "translation",
  "status": "running",
  "stage_fingerprint": "sha256:...",
  "selected_segment_count": 215,
  "requested_segment_count": 180,
  "reused_segment_count": 35,
  "started_at": "2026-07-29T12:00:00+08:00",
  "completed_at": null,
  "created_at": "2026-07-29T12:00:00+08:00"
}
```

Run 状态允许：

```text
running
completed
failed
interrupted
```

Run 创建时复制项目配置；LLM 阶段另保存本阶段实际完整 Prompt。续作不覆盖
原快照，而是在 `continuations/0001/` 等顺序目录保存本次当前项目配置和完整
Prompt，并在 manifest 追加本次指纹、原始范围和请求/复用数量。续作结果仍引用
原 `run_id`。

`terminology`、`translate`、`proofread`、`polish` 独立命令发现同阶段
`running` Run 时，最新一个是续作候选，更旧者标记为 `interrupted` 和
`superseded`。交互终端明确询问 `resume` 或 `new`；拒绝后候选记录
`resume_declined`，不再询问。非交互运行必须显式使用 `--resume-run` 或
`--decline-run`。

续作保留旧 Run ID、原始 scope 和术语 `active_task_id`，忽略本次范围参数与
`--force`；它使用当前项目配置、Prompt、模型、端点及代理，只请求旧范围中
尚无 completed 的 Segment。再次中断时 Run 保持 `running`。

## 3.5 阶段结果

翻译 completed 示例：

```json
{
  "schema_version": 1,
  "record_type": "stage_result",
  "record_id": "REC-...",
  "project_id": "PRJ-...",
  "stage": "translation",
  "segment_id": "F0001-S000101",
  "status": "completed",
  "text": "翻译结果",
  "validation_status": "passed",
  "stage_fingerprint": "sha256:...",
  "run_id": "RUN-...",
  "request_id": "REQ-...",
  "created_at": "2026-07-29T12:01:00+08:00"
}
```

失败示例：

```json
{
  "schema_version": 1,
  "record_type": "stage_result",
  "record_id": "REC-...",
  "project_id": "PRJ-...",
  "stage": "translation",
  "segment_id": "F0001-S000102",
  "status": "failed",
  "error_class": "format_error",
  "error_message": "Missing translation field",
  "stage_fingerprint": "sha256:...",
  "run_id": "RUN-...",
  "request_id": "REQ-...",
  "created_at": "2026-07-29T12:01:02+08:00"
}
```

校对和润色结果还保存：

- `review_status`：`accepted` 或 `suggested`
- `suggested_text`
- `reason`
- `base_result_id`

`review_status` 是阶段 payload，不等于记录的 `status`。结构有效的 accepted 和 suggested 都保存为 completed。
accepted 的持久化记录将 `suggested_text` 和 `reason` 规范化保存为 `null`。

Web 人工重置在同一阶段 JSONL 中追加 `status = "reset"`。reset 屏蔽该
Segment 此前的 completed；之后的新 completed 重新成为当前结果。重置校对或
润色时同时重置该阶段 applied 结果，但不级联删除其他阶段。

applied 结果保存：

- 实际输出文本
- 建议结果 ID
- 应用时的基准结果 ID
- 是否使用 `--allow-outdated-base`

---

# 4. 阶段流程

## 4.1 Prompt、上文与调度

四个 LLM 阶段使用：

```text
代码内固定 Prefix
+ 项目可编辑的 middle Prompt
+ 代码内固定 Suffix
```

固定部分定义角色、输入输出结构、适用阶段的 Segment ID 约束、参考上文边界和严格 JSONL 要求。每个非空物理行只能包含一个紧凑 JSON 对象，所有阶段最后一行必须为 `{"type":"end"}`。middle Prompt 只承载项目背景、文体、翻译习惯、术语偏好、校对或润色标准。

四阶段分别读取自己的：

```toml
enabled = true
previous_segments = 3
```

数量表示当前文件中、当前 Chunk 首个 Segment 之前最近的非空 Segment 数。

规则：

- 上文不得跨文件。
- 一个 Chunk 共享一份 `reference_context`。
- 上文不属于本次进度范围，不要求 LLM 输出。
- 失败或未完成 Segment 的源文仍可作为上文。
- 失败结果不能作为可信目标文本上文。
- 术语上文始终只含源文。

`ordered_by_file`：

- 同一文件内 Chunk 顺序执行。
- 不同文件可并发。
- 翻译、校对和润色上文可包含此前已有的阶段文本。

`parallel`：

- 所有 Chunk 可并发。
- 上文只含源文，不依赖当前 Run 尚未产生的结果。

术语提示词必须说明：参考上文只用于判断人物性别、指代、身份和上下文含义，不要仅因词语只出现在上文中就提取。

## 4.2 术语

### 发布库与活动任务

术语目录只维护：

- 一份已发布 `terms.json`。
- 一个 `active_task_id`。
- 追加式 scans 和 candidates。
- 一份人工 `overrides.json`。

已发布库包含递增整数：

```json
{
  "schema_version": 1,
  "record_type": "terminology_library",
  "record_id": "TERMS-7",
  "project_id": "PRJ-...",
  "terms_revision": 7,
  "published_run_id": "RUN-...",
  "terms": []
}
```

活动任务规则：

1. 普通 `terminology` 继续当前活动任务。
2. 没有活动任务且没有已发布库时，创建第一个任务。
3. 每个非空 Segment 在活动任务中记录 completed 或 failed 扫描。
4. 中断恢复只处理该任务中 pending 和 failed 的 Segment。
5. Chunk 变化不影响扫描进度。
6. 选定范围的设置指纹变化时先取得复用确认；pending 使用当前设置。
7. 所有非空 Segment 扫描 completed 后，合并候选、应用 overrides，并原子发布。
8. 发布成功后 `terms_revision` 加一。

`terminology --force` 创建新的全量活动任务，忽略此前扫描进度。上一份术语库
在新任务发布前继续可用；发布时新候选合并到上一份术语库，未再次发现的旧术语
不会自动删除。术语移除只能通过人工 disabled override 完成。旧任务记录可以
留在追加文件中，但不再参与产品逻辑，也不提供历史代次管理功能。

每条 scan 和 candidate 都记录 `active_task_id`；读取和发布时只使用 `active_task.json` 当前指向的任务记录。

### 候选和合并

术语请求不向 LLM 暴露 Segment ID：

```json
{
  "target_language": "简体中文",
  "reference_context": [
    {"source": "此前原文"}
  ],
  "source_segments": [
    {"source": "当前待扫描原文"}
  ]
}
```

`reference_context` 和 `source_segments` 都只含 source。扫描范围与 Segment ID 由程序内部持有；LLM 不需要也不得返回来源 Segment 引用。term 记录的 source 必须填写 `source_segments` 原文中实际出现的术语文本。

LLM 返回 JSONL；每个术语一行：

```jsonl
{"type":"term","source":"Silver Knight","category":"人物称号","description":"银发骑士的称号","preferred_translation":"白银骑士","aliases":["The Silver Knight"]}
{"type":"end"}
```

LLM 不需要声明术语属于哪个 Segment。合法术语行可以先保存为候选；只有所有行合法且最终存在 end 时，请求覆盖的每个 Segment 才记录扫描 completed。否则格式修正仍重试原请求范围。

归一化：

```python
normalized = unicodedata.normalize("NFKC", value)
normalized = normalized.casefold().strip()
```

候选以 normalized source 去重：

- 合并 aliases 和相同说明。
- 推荐译名或类别冲突时保留冲突信息，MVP 不自动裁决。
- 未解决冲突可以注入来源、候选类别和说明，但不注入歧义推荐译名。

alias 与另一条术语的主 source 相同时，由
`terminology.alias_primary_collision` 决定：

- `conflict`（默认）：保留两条主术语并标记冲突；碰撞 alias 不参与注入。
- `merge`：声明 alias 的术语吸收另一条主术语；元数据不一致继续进入冲突。

循环 alias 或多个术语争用同一主条目无法安全自动合并，始终进入人工冲突。

人工 override 以 normalized source 定位：

```json
{
  "normalized": "alice",
  "category": "女性人名",
  "preferred_translation": "爱丽丝",
  "description": "人工确认",
  "aliases": ["Alice"],
  "disabled": false
}
```

override 在自动合并后应用。`disabled = true` 的术语不发布、不匹配也不注入。

发布时可以按确定性排序重新分配只在当前库内有效的记录 ID，不承诺跨 revision 稳定。

### 术语交换

`terms-import` 和 `terms-export` 只接受 `.json`、`.csv`。JSON 顶层固定为
`schema_version = 1`、`record_type = "terminology_exchange"` 和 `terms`。
术语字段为 source、preferred_translation、category、description、aliases、
disabled，以及类别和推荐译名冲突候选。CSV 使用同一字段集合，数组字段保存为
JSON 数组字符串，导出编码为带 BOM 的 UTF-8。

导入在完整校验后按 normalized source 合并到自动扫描基线；文件缺项不删除
现有术语，人工 override 始终优先。`disabled = true` 是显式人工移除；
`disabled = false` 不自动撤销项目中已有的 disabled override。无实际变化时
不增加 revision。

### 翻译时匹配

程序对每个 Segment 的源文做相同归一化，然后用主名称和 aliases 进行子串匹配。

注入优先级：

1. 主名称命中。
2. alias 命中。
3. 更长的词优先。
4. 有推荐译名者优先。
5. 每个 Segment 最多 `max_terms_per_segment`。

不持久化 occurrence 文件或逐 Segment 术语 Hash。每次构建翻译请求时重新确定匹配术语。

## 4.3 翻译

翻译输入最少包含：

```json
{
  "target_language": "简体中文",
  "reference_context": [
    {"id": "F0001-S000097", "source": "...", "translation": "..."}
  ],
  "terms": [
    {
      "source": "Silver Knight",
      "preferred_translation": "白银骑士",
      "category": "人物称号",
      "description": "银发骑士的称号"
    }
  ],
  "segments": [
    {"id": "F0001-S000101", "source": "..."}
  ]
}
```

输出 JSONL：

```jsonl
{"type":"segment","id":"F0001-S000101","translation":"..."}
{"type":"end"}
```

映射规则：

- 只接受请求范围内的 Segment ID。
- 不根据返回顺序或数量猜测对应关系。
- 未知或多余 ID 忽略并记录。
- 重复 ID、缺少 ID 或字段错误只使对应 Segment 保持未决。
- end 前结构有效的 Segment 立即逐条保存，即使响应随后截断也不回滚。
- 后续格式修正只请求未决 Segment。

没有已发布术语库时允许直接 translate，但必须醒目警告，并在 Run 中记录 `terms_revision = null`。`run-all` 会先完成术语任务再翻译。

### 翻译文字校验

两个校验器可独立启用：

- `japanese_kana`：Hiragana、Katakana、Katakana Extensions、半角片假名及 Kana 扩展块。
- `korean_hangul`：Hangul Syllables、Jamo、Compatibility Jamo 和扩展块。

校验发生在结构解析成功之后、写 completed 之前。

命中时记录：

- 校验器名称。
- 命中字符和 Unicode code point。
- 字符位置。
- 候选文本。

普通模式只在最终 failed 或 warning 结果中保存必要校验信息；调试模式保存每轮候选和请求血缘。

修复流程：

1. 汇总当前轮校验失败 Segment。
2. 同一文件内按源文顺序分组；中间只有空行时仍可组成一个修复 Chunk。
3. 正常非空行或筛选边界中断分组。
4. 修复请求只包含失败 Segment、源文、失败候选、命中字符、相关术语和允许的上文。
5. 超过 Token 限制时继续拆分。
6. 每轮修复后重新执行全部已启用校验器。

耗尽后：

- `fail`：保存 failed，候选不成为当前翻译。
- `warning`：保存 completed 和 `validation_status = "warning"`，允许进入下游，但 inspect 和导出必须报告。

## 4.4 校对

启动前检查选定范围内全部非空 Segment。任何 Segment 缺少 completed 翻译时，整个校对命令不创建 Run 并返回未完成范围。

输入包含：

- 原文。
- 当前最新翻译。
- 相关术语。
- 允许的同文件上文。
- 校对 Prompt。

输出 JSONL：

```jsonl
{"type":"segment","id":"F0001-S000101","status":"accepted"}
{"type":"segment","id":"F0001-S000102","status":"suggested","suggested_text":"完整建议译文","reason":"遗漏否定含义"}
{"type":"end"}
```

accepted 表示无条件保留当前基准，模型输出无需包含 `suggested_text` 和
`reason`；端点若返回这两个字段，本地无论其值和类型如何均直接忽略，并在
持久化时规范化为 `null`。suggested 必须包含非空字符串 `suggested_text`，
`reason` 只允许字符串或 `null`。

每条结果保存本次使用的翻译 `base_result_id`。

## 4.5 润色

启动前为每个选定非空 Segment 选择基准：

```text
最新 proofreading_applied
→ 最新 translation
```

任意 Segment 没有可用基准时，整个润色命令不创建 Run。

输出结构与校对相同，状态只允许 accepted 或 suggested。每条结果保存 `base_result_id`。

如果没有应用校对，润色直接以翻译为基准。`run-all` 因为不隐式 apply，所以校对和润色建议默认都独立基于翻译。

## 4.6 apply

命令：

```bash
python -m app.main apply PROJECT --stage proofreading --all
python -m app.main apply PROJECT --stage polishing --all
```

`--all` 是必需的批量确认，可以与文件或 Segment 范围组合。

应用规则：

- suggested：输出完整 `suggested_text`。
- accepted：输出建议记录所使用的基准文本。
- 不覆盖翻译或建议历史，写入独立 applied JSONL。

apply 前必须确认建议的 `base_result_id` 仍是该 Segment 当前选择的基准。若上游已经变化：

- inspect 和普通阶段只警告。
- apply 默认拒绝整个选定范围。
- `--allow-outdated-base` 可显式强制，并在 applied 结果和日志中记录警告。

选定范围内任何非空 Segment 缺少 completed 建议或对应基准时，整个 apply 不创建 Run，也不应用可用子集。

## 4.7 run-all

默认顺序：

```text
术语
→ 翻译
→ 校对建议
→ 润色建议
```

不隐式 apply。

规则：

- 没有已发布术语库时，先完成活动术语任务。
- 存在未完成的活动术语任务时，先继续并发布，再翻译。
- 已有完整术语库且没有活动任务时直接复用。
- 设置指纹不同时逐阶段确认复用；拒绝后停止，不自动重做。
- 任一阶段选定范围未完成时停止，不启动依赖它的后续阶段。
- `--force` 明确要求所选阶段重做；用于术语时创建新的全量活动任务。

---

# 5. 执行可靠性

## 5.1 Token 与 Chunk

完整渲染 Prompt 必须满足：

```text
safe_estimate(rendered_prompt)
<= context_window_tokens
   - context_safety_margin_tokens
```

`max_output_tokens` 是提交给 Chat Completions 的输出上限，不作为每次请求必然
占用的固定预留量。每次请求实际发送：

```text
effective_max_tokens
= min(
    max_output_tokens,
    max(
        1,
        context_window_tokens
        - safe_estimate(rendered_prompt)
        - context_safety_margin_tokens
    )
  )
```

发生自动收窄时，本 Run 记录 warning，但不因配置的输出上限过大而拒绝启动。

Chunk 目标同时受 `target_chunk_input_tokens` 约束。估算必须覆盖：

- 固定 Prompt。
- middle Prompt。
- 输出结构说明。
- 目标语言。
- 上文。
- 术语。
- 当前 Segment。
- JSONL 记录结构字符。

`token_safety_factor` 必须大于 `0`，可以小于、等于或大于 `1`。小于
`1` 会主动降低启发式估算值，可提高分词效率更高模型的上下文利用率，但也会
增加服务端上下文超限或实际 ITPM 超限风险。该估算不保证与任一模型、语言或
Prompt 的真实分词结果一致，使用者应依据实际分词器和端点行为调节。

每次尝试加入 Segment 后重新渲染并估算完整 Prompt。Chunk 参数只影响本次请求组合，不影响任何已完成 Segment。

`target_chunk_input_tokens` 是软目标。贪心累计时，加入下一个 Segment 将超过目标便结束当前 Chunk；单个 Segment 自身超过目标但仍低于模型输入硬限制时，可以单独发送。文件末尾和范围末尾的短尾 Chunk 允许明显低于目标。

配置或请求在发送前失败的情况：

- 固定 Prompt 已超过硬限制。
- 上下文窗口无法容纳输入和安全余量。
- 单个实际请求的预测输入 Token 超过 ITPM。
- 单 Segment 即使拆到最小 part 仍无法容纳。

超长单 Segment：

1. 优先按段落类标点切分。
2. 再按句号、问号、感叹号。
3. 再按逗号、分号。
4. 最后按字符数硬切。
5. part 仍属于原 Segment，结果按顺序合并后只写一条 Segment 结果。

关闭 `allow_split_oversized_segment` 时直接记录该 Segment failed。

## 5.2 HTTP、并发与限流

请求 URL 固定由项目配置组成：

```http
POST {base_url}{endpoint}
Content-Type: application/json
```

Header、完整 JSON body 和成功响应正文路径由选中的 JSON LLM Adapter 定义。
内置 `openai-compatible` 使用 Bearer API Key、Chat Completions body 和
`/choices/0/message/content`。声明式 Adapter 只支持非流式 JSON POST。

整个命令共享一个 `httpx.AsyncClient`：

- connect、read、write 和 pool timeout 都使用 `request_timeout_seconds`。
- 连接池上限从 `max_parallel` 派生。
- `asyncio.Semaphore` 控制并发。
- 显式代理使用 `proxy=proxy_url`；空值不关闭 HTTPX 的标准环境代理。

RPM 和 ITPM 使用单进程 60 秒滑动窗口。检查与预约由同一个异步锁保护。每次实际 HTTP 尝试都重新预约额度，失败后不返还。

## 5.3 重试与部分响应

HTTP 重试：

- 408、429、5xx、连接错误和读取超时可重试。
- 429 优先使用 `Retry-After`。
- 400、401、403、404 和其他不可恢复 4xx 不重试。
- 401、403 或明显错误端点停止当前阶段尚未发送的任务。
- 所有 HTTP 错误共享 `http_max_attempts` 总上限。
- 退避使用有上限的指数退避和 jitter。

格式修正：

- Adapter 提取出的 content 开头允许存在一个完整的 Tag 思考块：
  `<think>...</think>`、`<thinking>...</thinking>`、
  `<thought>...</thought>` 或 `<analysis>...</analysis>`。Google AI Studio
  兼容端点实测使用 `<thought>`。允许思考块前有 BOM 或空白；剥离后再按
  下述 JSONL 规则解析。
- 只剥离开头一个完整的已知思考块。未闭合、重复、嵌套或不在开头的标签不得
  猜测或全文删除，按普通格式错误处理；JSON 字符串字段内的同名文本保持原样。
- 思考正文不属于 Prompt、Chunk、Segment 结果或进度。普通模式不持久化；
  debug 模式仍在原始响应 Payload 中保存，不新增独立思考记录。
- 原始正文或受支持 Markdown 围栏内部必须是一行一个 JSON 对象；围栏外说明文字忽略。
- 接受 `jsonl`、`ndjson`、`json` 和无标签围栏，不接受旧顶层 JSON 对象或数组协议。
- 每行独立解析；立即保存 end 前 ID 唯一、字段有效且通过校验的 Segment。
- end 必须存在且为最后一条记录；缺少或提前 end 会进入格式修正，但已保存 Segment 不回滚。
- 缺失、重复或字段错误的 Segment 按共享 Chunk 规则重新分组；空行不切断，已成功的非空 Segment 会切断其两侧未决项。
- 新请求只包含未决 Segment。
- 术语响应只有在无行级错误且以 end 结束时才推进扫描完成状态。
- 格式修正最多 `format_max_attempts` 轮。

模型报告上下文过长时，Chunk 对半拆分；单 Segment Chunk 进入内部 part 切分。

翻译文字校验使用自己的 `max_retry_attempts`，不与 HTTP 或格式重试相加。

## 5.4 持久化与中断恢复

普通 JSON 使用同目录临时文件写完后原子替换。

所有承载进度的 JSONL，包括阶段结果、术语 scans 和 candidates：

- 由单写入协程或进程内异步锁串行追加。
- 每条记录完整写入换行后执行 flush 和 `fsync`。

读取 JSONL：

- 中间行损坏：报告存储完整性错误并停止。
- 最后一行损坏：将损坏尾部保存为带时间戳的 `.corrupt-tail`，截断至最后完整记录后继续。

`--dry-run` 遇到损坏尾行时只报告，不执行备份或截断，以保持零写入。

恢复只读取：

- 每个 Segment 是否存在 completed。
- 没有 completed 时最近是否 failed。
- 活动术语任务中的 Segment scan 状态。

Segment 进度恢复不读取 Chunk ID、Chunk Manifest 或 Request 状态。Run 状态
只用于发现可续作的执行身份和旧 scope；实际 pending 仍完全由 Segment 结果或
术语 scan 推导。

## 5.5 普通日志与调试模式

普通模式保存：

- Run Manifest 和快照。
- Segment 阶段结果。
- 术语任务进度与候选。
- 人类可读 `app.log`。

CLI 无论 debug 是否启用都将带时间、级别和阶段的实时日志写入 stderr，并把相同基本日志写入 `app.log`；最终命令汇总单独以 JSON 写入 stdout。日志不得包含正文、译文、Prompt、API Key 或完整 Payload。

普通模式不保存：

- Chunk Manifest。
- 逐 Attempt 结构化日志。
- 完整请求、响应和错误 Payload。
- 中间校验候选。

`debug.enabled = true` 时额外保存：

- 当前 Run 的 `chunks.jsonl`。
- 每次 Attempt 的结构化日志。
- 每次实际请求、响应或错误 Payload。
- 格式修正、上下文拆分和校验修复的父请求关系。
- 每轮翻译校验候选。

保存 Payload 时必须移除 Authorization、API Key、Cookie 和 Set-Cookie。

故障注入只在 debug 模式生效，计数配置为 0 时关闭对应注入。

---

# 6. CLI、inspect 与导出

## 6.1 命令

```bash
python -m app.main init INPUT... --name PROJECT_NAME
python -m app.main init BOOK.epub --name PROJECT_NAME --document-adapter epub
python -m app.main inspect PROJECT
python -m app.main terminology PROJECT
python -m app.main terms-import PROJECT terms.json
python -m app.main terms-export PROJECT terms.csv
python -m app.main translate PROJECT
python -m app.main translate PROJECT --resume-run
python -m app.main translate PROJECT --decline-run
python -m app.main proofread PROJECT
python -m app.main polish PROJECT
python -m app.main apply PROJECT --stage proofreading --all
python -m app.main apply PROJECT --stage polishing --all
python -m app.main export PROJECT --stage translated
python -m app.main export PROJECT --stage proofread
python -m app.main export PROJECT --stage polished
python -m app.main export PROJECT --stage translated --bilingual
python -m app.main run-all PROJECT
```

通用范围参数：

```text
--from-file F0002
--only-file F0002
--only-segment F0002-S000123
```

三者互斥。

行为参数：

```text
--dry-run
--force
--allow-outdated-base
--allow-missing
--bilingual
--all
--resume-run
--decline-run
--reuse-mixed-fingerprints
--document-adapter
```

语义：

- `--force`：重做选定范围内所有非空 Segment，覆盖普通 pending/failed 筛选。
- `--dry-run`：不写文件、不创建 Run、不更新模板、不调用 LLM，只报告范围、设置警告、Chunk 和 Token 估算。
- `--allow-outdated-base`：仅用于 apply，允许应用基于旧上游结果的建议。
- `--allow-missing`：仅用于 export，允许使用阶段回退。
- `--bilingual`：仅用于 export。
- `--all`：apply 的必需批量确认。
- `terms-import` 根据 `.json` 或 `.csv` 扩展名增量导入术语；`--dry-run`
  只校验并报告变化。
- `terms-export` 根据输出扩展名导出术语；默认不含 disabled，
  `--include-disabled` 用于完整备份人工移除决定。
- `--resume-run`：仅用于四个独立 LLM 阶段，续用最近同阶段 running Run。
- `--decline-run`：仅用于四个独立 LLM 阶段，明确结束该候选并创建新 Run。
- `--reuse-mixed-fingerprints`：显式复用选定范围内设置指纹不同的 completed；
  仅用于四个 LLM 阶段和 `run-all`，并与 `--force` 互斥。
- `--document-adapter`：仅用于 init，选择输入与输出格式，默认 `txt`。

两项续作参数互斥。没有候选时 `--resume-run` 是用法错误；
`--decline-run` 直接按当前范围创建新 Run。`--dry-run` 不询问、不修改
manifest；与 `--resume-run` 合用时按旧范围生成续作计划，但不写 continuation。

删除：

```text
--include-stale
--allow-stale
--update-from-global
--keep-project-version
```

稳定退出码：

```text
0    成功；允许普通 warning 和 validation warning
1    未分类内部错误
2    CLI 用法、配置或模板错误
3    项目、schema 或存储完整性错误
4    鉴权、端点等阶段级致命外部错误
5    选定范围仍有 pending 或 failed
130  用户中断
```

## 6.2 inspect

inspect 至少报告：

- 文件数、总 Segment 和空 Segment。
- 当前已发布 `terms_revision`。
- 活动术语任务的 completed、failed、pending。
- 翻译、校对、润色和 applied 的 completed、failed、pending。
- 当前设置指纹与已有结果指纹的差异及混合来源数量。
- 有旧 completed 但最近重做失败的 Segment 数。
- validation warning 数。
- 基于旧上游的校对或润色建议数。
- 当前仍为 running 的 Run ID、阶段、开始时间和原始范围。
- 是否存在新的全局模板。
- 建议的下一条命令。

inspect 不修改 Run 状态或用户进度。JSONL 损坏尾行恢复属于存储维护。

## 6.3 导出

阶段映射：

```text
translated → 最新 translation completed
proofread  → 最新 proofreading_applied completed
polished   → 最新 polishing_applied completed
```

输出目录：

```text
output/translated/
output/proofread/
output/polished/
output/bilingual/translated/
output/bilingual/proofread/
output/bilingual/polished/
```

尚未 apply 的 accepted/suggested 建议不能直接作为 proofread 或 polished 导出结果。

通用行为：

- 缺少选定阶段结果时停止并报告。
- validation warning 和混合设置必须出现在导出摘要。
- 使用项目初始化时的 Document Adapter；插件缺失、版本不兼容、状态损坏或
  能力不足时明确失败，不回退为 TXT。
- Adapter 先在宿主临时目录生成全部文件，成功后原子移动到正式输出目录。

TXT 按 `file_order` 和 `line_index` 重建，每个输入文件独立导出，并使用
`project.output_encoding` 严格编码。编码无法表示结果字符时失败，不静默替换。
EPUB 输出一个 `.translated.epub` 或 `.bilingual.epub`，只重写翻译对应的
XHTML 槽位。

`--allow-missing` 回退：

```text
translated → source
proofread  → translation → source
polished   → proofreading_applied → translation → source
```

所有回退 Segment 写入摘要。

单语模式：

- 每个源 Segment 对应一个目标文本逻辑行。
- 空 Segment 对应空行。

双语模式：

- 非空 Segment 先写原文，再写目标文本。
- 空 Segment 只写一个空行。
- translated、proofread 和 polished 都支持。
- 回退文本仍写在原文下一行。

TXT 可以使用任意一致的文本行分隔方式。验收不检查换行符种类、BOM、末尾换行
或输出字节，只检查逻辑行和可见空行结构。EPUB 验收检查 spine 顺序、XHTML
结构及未修改资源仍可读取。

## 6.4 本地 Web Alpha

`python -m app.web` 只允许绑定 `127.0.0.1` 或 `localhost`。HTTP 层拒绝非
本机 Host 和跨站 Origin。Web 可创建 TXT/EPUB 项目、编辑项目配置、Prompt 与
JSON LLM Adapter、运行/取消阶段任务、人工审校、apply 和 export。

Web 不建立数据库；项目目录与 CLI 使用相同应用内核和持久化记录。同一项目的
写任务通过非阻塞文件锁互斥，冲突时明确失败。后台任务状态只存在于当前 Web
进程，重启后仍由项目 Run 与 Segment 记录恢复业务进度。

Web 只在术语、翻译、校对和润色页面提供阶段启动入口。每次启动前读取当前阶段
统计和最近的 running Run：存在未完成 Run 时必须明确续用或结束后新建；存在
不同设置指纹的 completed 时必须明确复用或 force。force 与复用互斥，续用 Run
时不接受会被忽略的 force 或复用参数。所有决策在创建后台任务和阶段 Run 前
再次校验；并发修改导致条件变化时明确失败，不自动降级。桌面和窄屏使用同一
运行决策流程。

术语页支持 JSON/CSV 导入导出、经典 Ctrl/Cmd/Shift 多选和批量移除。翻译、
校对、润色列表使用相同多选规则；批量清除采用追加 reset。校对和润色可应用
所选或当前过滤范围，缺建议或缺基准时整批拒绝，旧基准必须显式允许。批量
清除不会改变阶段运行 scope；随后启动仍处理项目内全部 pending/failed。

---

# 7. 核心验收矩阵

## 7.1 输入与输出

测试数据包含：

- UTF-8、带 BOM UTF、GB18030 和可由 fallback 解码的 TXT。
- 多文件、自然排序、递归子目录和不同目录同名文件。
- 首部空行、连续空行、尾部空 Segment 和空文件。
- 编码无法表示目标译文的失败样本。

验收：

- 文件和 Segment 顺序正确，不跨文件混合。
- 解码失败明确拒绝，不产生替换字符。
- 单语导出每个 Segment 对应一个逻辑行。
- 三个阶段的双语导出均遵守原文/目标文本规则。
- 未配置时使用 `utf-8-sig`，显式配置的其他输出编码生效。
- 输入编码不影响输出编码选择。
- 输出可由常用查看器正确显示。
- `--allow-missing` 回退和摘要正确。
- 不比较输入输出的换行符、BOM、末尾换行、编码或字节。

## 7.2 阶段流程

验收：

- 四阶段上文数量分别生效且不跨文件。
- 术语上文只含源文，不计入扫描进度。
- ordered_by_file 和 parallel 的上文内容符合定义。
- 术语按 normalized source 合并，override 和 disabled 生效。
- 活动术语任务可续作，force 新任务完成前不替换已发布库。
- 翻译 ID 映射严格，空 Segment 不请求。
- 日语假名和韩语谚文校验覆盖配置的 Unicode 块。
- 校对和润色只在完整上游范围存在时启动。
- accepted/suggested 与记录 completed/failed 正确分离。
- apply 默认拒绝旧基准建议，显式参数可以强制。
- run-all 不隐式 apply。

## 7.3 恢复、Chunk 与重试

验收：

- 每个 Chunk 只含同文件、保持源文顺序的待处理非空 Segment。
- Chunk 可以跨空行，但不能跨已完成、范围外或其他未处理的非空 Segment。
- 部分响应保存中间 Segment 后，其两侧未决项重新拆成独立 Chunk。
- 修改 Chunk 大小、并发、限流或调度后不丢失 Segment 进度。
- 中断后 completed 不重复请求，pending 和无成功结果的 failed 继续处理。
- 四个独立 LLM 阶段能发现并续用相同 Run ID；旧 scope 和术语任务保持不变，
  当前配置与 Prompt 写入新的 continuation 快照。
- 拒绝候选后不再询问；非交互参数和 dry-run 零写入语义正确；多个 running
  Run 中更旧者被 supersede。
- 强制重做失败不遮蔽旧 completed，但命令返回退出码 5。
- 合法部分响应立即保存有效 Segment。
- 原始 JSONL、CRLF、BOM、空行、受支持 Markdown 围栏和已知开头思考块均可解析。
- 未闭合、重复、嵌套或不在开头的思考标签会进入格式修正，JSON 字段内标签文本
  保持原样。
- 缺失、重复或提前 end、非法行、重复或未知 ID 会进入格式修正。
- 旧顶层 JSON 对象或数组不再接受。
- 格式修正和校验修复只请求连续分组后的未决 Segment。
- HTTP、格式和校验重试均不会超过各自总上限。
- 显式 HTTP/HTTPS 代理传给 HTTPX；空代理保留标准环境代理行为，非法协议拒绝。
- 401/403 停止尚未发送的任务。
- JSONL 尾行损坏可恢复，中间行损坏明确停止。
- debug 开关两种状态下 stderr 和 `app.log` 都有基本运行日志，stdout 仍是可独立解析的最终 JSON。

## 7.4 用户设置决策

验收：

- Prompt、模型、目标语言、上下文、校验策略或术语 revision 变化时，交互
  环境询问是否复用；非交互环境要求显式 flag。
- 用户确认或指定 `--reuse-mixed-fingerprints` 后已有 completed 继续复用。
- 新 pending 使用当前设置。
- inspect 能报告同一阶段的混合设置来源。
- 只有 `--force` 才重做已有 completed。
- Chunk、Token、并发、限流、timeout 和重试变化不进入阶段指纹。
- 调度模式会改变可用上文内容，因此进入阶段指纹并要求相同确认。

## 7.5 模板、普通模式与调试

验收：

- init 正确复制全局模板。
- 新全局 Bundle 在交互模式询问一次，更新前备份，保留时不覆盖。
- 非交互模式只警告且不改变 seen Hash。
- dry-run 对模板和项目均零写入。
- 普通模式不生成 Chunk、Attempt 和完整 Payload 文件。
- 调试模式保存 Chunk、Attempt、Payload 和父请求关系。
- 调试故障注入不会在 `debug.enabled = false` 时生效。
- 普通和调试模式都能通过 Segment 结果恢复。

## 7.6 Adapter、EPUB 与 Web

验收：

- JSON LLM Adapter 的类型化占位符、自定义嵌套字段、认证 Header 和 RFC 6901
  响应路径生效；非法 schema、未知占位符、缺失路径和非字符串正文快速失败。
- Adapter 定义副本与 Run 快照不包含 API Key，定义 Hash 进入阶段指纹。
- TXT 旧项目没有 Document Adapter 字段时仍按 `txt` 导出。
- EPUB 保持 spine 顺序、跨节点 Segment 定位、导航、元数据和非翻译资源；
  纯译文和双语文件均可重新打开。
- EPUB ZIP 路径、符号链接、压缩炸弹和 XML DTD/实体输入明确拒绝。
- Document Adapter 缺失、版本不兼容、状态损坏或运行失败时不发布部分输出，
  也不静默回退。
- Python 插件发现拒绝重复 ID 和未知协议版本。
- Web 只接受本机 Host/Origin，与 CLI 共用项目记录；同项目第二个写任务明确
  失败，取消后的 Run 有正确收尾。
- React 生产构建、TypeScript 检查、桌面与窄屏关键交互、浏览器控制台均通过。

---

# 8. 最终最小架构

```text
TXT / EPUB
   │
   ▼
稳定 File / Segment
   │
   ├───────────────┐
   ▼               ▼
活动术语扫描     阶段结果历史
   │               │
   └──────┬────────┘
          ▼
项目配置与 Prompt 副本
          │
          ▼
Run 原始快照与续作快照
          │
          ▼
同文件连续临时 Chunk
          │
          ▼
声明式 JSON LLM Adapter
```

最终原则：

```text
File 是内容边界。
Segment 是唯一进度和恢复单位。
Chunk 只是当前请求包装。
Run 记录一次执行。
设置变化不使旧结果失效；复用或重做由用户明确决定。
普通模式保存最小结果，调试模式增加请求审计。
TXT 保持逻辑行；EPUB 保持原包资源和格式定位。
```
