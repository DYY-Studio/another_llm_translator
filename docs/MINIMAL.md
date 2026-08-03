# 基于 LLM 的工程化翻译器

## 最小可行性验证实现文档

版本：MVP 0.3

实现语言：Python 3.11+

运行方式：单机、本地 CLI/Web、异步并发

存储方式：项目文件夹、SQLite、JSON、TOML、TXT、EPUB

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

### File 与文档 part 是内容边界

File 仍是存储、选择和导出的边界；任何 LLM 请求、Chunk 和参考上文都不得跨
`(file_id, part_id)`。术语库可以在项目级汇总，但单次术语请求仍不得跨文档
part。TXT 和普通 Adapter 使用 `part_id = "document"`；EPUB 使用每个 spine
XHTML 的归档路径作为 part。调度仍按 File 进行，不把 EPUB 拆成多个 File。

### Segment 是进度单位

Segment 是 Document Adapter 返回的有序可翻译单元：TXT 中对应逻辑行，EPUB
中对应 XHTML 文本流。每个 Segment 记录非空的 `part_id`；普通透明内联元素中的相邻文本槽合并为一个语义单元；
Ruby 是同一文本流中的内联语义成员，与前后普通文本共同组成一个语义单元；只有
没有相邻文本的独立 Ruby 才保持旧的独立定位形状。术语扫描、翻译、校对和润色的完成结果、
失败记录和恢复判断全部绑定 `segment_id`。

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

- 同一个 `file_id` 和 `part_id`。
- 按 `line_index` 保持顺序的非空 Segment。
- 当前命令选定范围内仍需处理的 Segment。

两个待处理非空 Segment 之间只有一个或多个空 Segment 时可以进入同一 Chunk；空 Segment 本身仍不提交 LLM。已完成、筛选范围外、不同 part 或其他不在待处理集合中的非空 Segment 会结束当前 Chunk。部分响应、格式修正和翻译校验修复也必须使用相同规则重新分组。

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
- 消息队列或分布式执行。
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
CLI、异步调度、SQLite/JSON/TOML、Hash、日志、路径、原子替换、插件发现、
EPUB ZIP/XML 处理和 Unicode 归一化。

代码只需覆盖以下职责，不预先规定模块数量：

- CLI 和配置加载。
- 项目初始化及模板同步。
- TXT/EPUB Document Adapter 与 Segment 化。
- SQLite 项目持久化与项目外 JSON 交换文件。
- Prompt 渲染、Token 估算和 Chunk 生成。
- 声明式 LLM Adapter、HTTP 调用、限流和重试。
- 术语、翻译、校对、润色和 apply。
- inspect 与原文档格式导出。
- 只绑定本机回环地址的 Web Alpha。

## 2.2 项目内容

项目至少包含：

```text
project.sqlite
config.toml
prompts/
input/
（项目元数据、File、Segment、Adapter 状态、术语、阶段结果、Run 索引和活动任务
状态均在 project.sqlite 中）
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

TXT 支持目录或显式文件；EPUB Adapter 每次导入一个显式文件。项目也可先创建
不预设格式的空项目：

```bash
python -m app.main init INPUT... --name PROJECT_NAME
python -m app.main init INPUT_DIR --recursive --name PROJECT_NAME
python -m app.main init BOOK.epub --document-adapter epub --name PROJECT_NAME
python -m app.main init BOOK.epub --document-adapter epub --epub-ruby-mode aozora --name PROJECT_NAME
python -m app.main init --empty --name PROJECT_NAME
python -m app.main init --empty --name PROJECT_NAME --parent-dir PARENT
python -m app.main files-add PROJECT INPUT...
python -m app.main files-add PROJECT INPUT_DIR --recursive
python -m app.main files-add PROJECT INPUT --document-adapter ADAPTER_ID
python -m app.main files-add PROJECT BOOK.epub --epub-ruby-mode base_only
python -m app.main files-remove PROJECT FILE_ID...
```

规则：

- init 必须在输入和 `--empty` 中恰好选择一种。
- init 默认写入内置 `projects/`；`--parent-dir` 在已存在、可写的明确父目录下
  创建项目。相对路径按当前工作目录解析，后续命令可直接使用项目绝对路径。
- 项目目录是自包含边界；选择外部位置不会移动或复制已有项目。
- 显式文件按参数顺序处理。
- 目录按相对路径进行确定性的简单自然排序。
- TXT 未传 `--recursive` 时只读取目录第一层。
- 递归发现时忽略符号链接。
- 显式符号链接输入直接拒绝。
- 多个输入映射到同一导出相对路径时拒绝初始化。
- 目录输入保存相对目录树；显式文件使用 basename。
- 输入文件复制到项目 `input/`，后续阶段只读取项目数据。
- 每个 File 记录来源 `document_adapter_id`、版本和可选不透明状态位置；旧项目
  的项目级字段在读取时解释为各活动 File 的来源，下一次文件变更时规范化。
- 空项目不锁定格式，可混合追加不同 Adapter 支持的文件。省略
  `--document-adapter` 时按 Adapter 声明的扩展名选择；目录可发现所有受支持
  格式，不支持文件被忽略并汇总提示。不同 Adapter 的扩展名声明不得重复。
- 新 File 追加到活动顺序末尾。`next_file_sequence` 单调增加；旧项目缺少该
  字段时从活动 File ID 最大值初始化。删除后重新添加不会复用 File 或 Segment
  ID。
- 活动文件的导出相对路径按大小写不敏感比较；追加产生冲突时整体拒绝。
- 文件增删发现任意 `running` Run 时整体拒绝，不自动中断。未知或部分非法的
  删除选择也必须在写入前整体失败。
- 增删在项目写锁内预写新索引、元数据和输入副本；普通发布异常不得留下部分
  更新。移除会删除项目内输入副本；Run、SQLite 阶段记录、术语和既有输出保留。
- 被移除 Segment 的历史阶段记录仍是审计数据，但不再参与 inspect、指纹差异、
  结果复用、校验警告、过期建议统计或导出。
- 移除 File 时同时清除该 File 独占的 Adapter 不透明状态；重新添加生成新状态。
- Web 项目概览提供“删除项目”操作。删除必须明确确认，且项目不存在未完成 Run；
  确认后删除整个自包含项目目录（输入、Run、术语库、阶段结果和导出），不可恢复。
  删除外部项目时同时清理本机 Web 会话中的最近路径记录。

空项目以及只含空白 Segment 的项目仍可打开、inspect、编辑配置、Prompt 和
Adapter，并可人工导入导出术语。术语、翻译、校对、润色、run-all、apply 和
export 必须在创建 Run 或请求前快速失败，提示先添加含非空 Segment 的文件。

EPUB Adapter 只接受 OPF `package` 版本 `2.0` 或 `3.0`，按 spine 顺序读取
XHTML 文本流。普通透明内联元素（例如 `span`、`em`、`strong`）中的相邻
`text`/`tail` 槽合并为一个 Segment；未知结构和 `br` 形成边界，槽之间的
非空白文本以及内部空白按源文保留。每个 spine XHTML 是一个 `part_id`，但仍
属于同一个 EPUB File；原 EPUB 及不透明定位状态用于重建输出；
导航、元数据、图片、CSS、字体和其他未翻译资源保持原样。ZIP 路径穿越、符号
链接、重复路径、异常条目数/解压大小/压缩比、越界资源以及非法 XML 会被拒绝。

EPUB 3 XHTML 允许省略 DOCTYPE，或使用不含外部标识的 `<!DOCTYPE html>`；
EPUB 2 XHTML 允许省略 DOCTYPE，或使用 PUBLIC
`-//W3C//DTD XHTML 1.1//EN` 的 XHTML 1.1 声明。外部 DTD 永不加载；实体声明、
不匹配版本的 DOCTYPE、SYSTEM-only 声明和不支持的 PUBLIC 标识均拒绝。

完整 `<ruby>` 子树和紧随的尾文本作为文本流中的一个成员；它与同一文本流中的
普通 `text`/`tail` 槽、其他 Ruby 按源文顺序组成一个 Segment。导入前可选择
`aozora`（默认，`｜原文《Ruby》`）、`base_only` 或 `parenthetical`
（`原文（Ruby）`）；选项固化于 File 的 Adapter 状态，不是项目运行设置。
更改既有文件的模式必须移除并重新导入，从而分配新的 File/Segment ID。纯译文
EPUB 将整条译文写入该语义 Segment 的首个可用位置，清空其余普通槽并移除全部
Ruby；双语 EPUB 保留完整源句和 Ruby，只在整个 Segment 末尾追加译文。使用
`ruby_mode=aozora` 时，模型可以自由决定是否保留 Ruby；严格的
`｜base《reading》` 会在译文区域恢复为 EPUB Ruby，reading 可翻译或转写为目标
语言适用的字母/注音。没有返回 Ruby 合法且不触发重试；不完整、嵌套、含 HTML
或跨行的形式保持普通文本。`base_only` 和 `parenthetical` 不还原 Ruby。
嵌套 Ruby、空读音和无法确定读音结构的输入会带 XHTML 位置拒绝。

EPUB 还支持独立的 `inline_format_mode`：默认 `plain` 不向模型暴露普通内联
标记；`markers` 为符合 `inline_format_policy` 的标签生成无 attrs 的唯一成对
标记，并把对齐文本保存为 Segment 的 `model_source`，而 `source` 保持净显示
文本。`tiered`（默认）要求语义关键标签保留、允许表现层标签整体省略；`strict`
要求全部源标签保留。翻译、校对、润色及参考上下文使用 `model_source`，术语使用
净 `source`。Adapter 在结果写入前验证标记的已知性、唯一性、闭合、嵌套和父子
关系；失败沿用格式修复预算，模型标记不会直接作为 HTML 写入，纯译文继续保留
原普通标签和 attrs 的空骨架。既有 File 的导入选项不会静默重切，修改后必须重新
导入；运行选项随 Adapter 状态和阶段指纹保存。

项目创建后，`project.sqlite` 是项目元数据、File、Segment、Adapter 状态、术语、
阶段结果和 Run 索引的唯一真相；原始输入、配置、Prompt、Run 快照、调试 Payload
和导出文件仍是项目外围文件。手工修改 SQLite 或项目 `input/` 均不受支持；需要
修改源文时重新创建项目。

`ImportedFile.segment_part_ids` 是与 `segments` 对齐的可选导入字段；省略时宿主
填入单一 `document` part。新项目必须为每条 Segment 持久化有效 `part_id`；读取
缺少、为空或不合法的旧数据时快速失败并提示重新创建项目，不读取 locator 猜测或
写入迁移。

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
- `part_id`
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

配置严格拒绝未知键。CLI 和执行流程继续从项目 `config.toml` 读取配置。本地
Web 不暴露完整 TOML 编辑器，而是读取和提交覆盖下列全部字段的类型化分组
表单；服务端通过同一严格校验后原子写入规范 TOML。非法类型、缺失或未知字段、
非法字段组合、Preset 缺失及项目不存在对应 LLM Adapter 均在写入前整体拒绝，
原配置保持不变。

```toml
[project]
target_language = "简体中文"
output_encoding = "utf-8-sig"

[input]
encoding_confidence_threshold = 0.60
fallback_encoding = "utf-8"

[llm]
preset = "default"
preset_terminology = ""
preset_translation = ""
preset_proofreading = ""
preset_polishing = ""

temperature_terminology = 0.1
temperature_translation = 0.2
temperature_proofreading = 0.1
temperature_polishing = 0.3

[execution]
scheduling_mode = "ordered_by_file"

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

Preset 中的 `requests_per_minute = 0` 和 `input_tokens_per_minute = 0` 分别表示
禁用 RPM 和 ITPM 限速。两者可以独立禁用；ITPM 为 0 时也不参与 Chunk 目标
及单请求 Token 上限判断。模型上下文窗口和 `max_parallel` 始终生效。

API Key 只从环境变量读取，不写入项目、Run、日志或 Payload。

`llm.preset` 选择全局默认的 `llm_presets/<id>.json`；四个
`llm.preset_<stage>` 可用非空 Preset ID 覆盖对应阶段，空字符串继承全局值。
Preset 再选择全局
`llm_adapters/<id>.json`；项目不保存 Adapter 副本。Adapter 定义是完整的
Header/body 模板和成功响应
JSON Pointer；Preset 的 `extra_body` 可追加无冲突的 Provider 自定义字段。
两者的内容 Hash 都进入阶段指纹，定义副本都进入 Run 快照，解析后的密钥不进入
任何持久化内容。完整 schema 见 `docs/ADAPTERS.md`。

Preset 的 `proxy_url` 为空时不显式设置代理，HTTPX 仍按默认行为读取 `HTTP_PROXY`、
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

`init` 将全局配置和四个提示词复制为项目工作副本。项目配置和 Prompt 使用
项目副本；LLM 连接通过项目保存的 Preset ID 实时读取全局 `llm_presets/` 与
`llm_adapters/` 定义，不保存 Adapter 副本。

项目只记录：

```text
global_bundle_hash_seen
```

Bundle Hash 对全局配置和四个提示词的相对路径及内容计算。它只用于发现新的
全局模板，不包含实时 Preset 内容，也不参与阶段结果判断。

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
- LLM Preset ID 与定义内容 Hash

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

Run 创建时复制项目配置；LLM 阶段另保存本阶段实际完整 Prompt、解析后的
`llm_preset.json` 和项目 `llm_adapter.json`。续作不覆盖原快照，而是在
`continuations/0001/` 等顺序目录保存本次当前项目配置、Prompt、Preset 和
Adapter 快照，并在 manifest 追加本次指纹、原始范围和请求/复用数量。续作
结果仍引用原 `run_id`。快照只记录 API Key 环境变量名，不读取或保存密钥值。

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

固定部分定义角色、输入输出结构、请求内短 ID 约束、参考上文边界和严格 JSONL 要求。每个非空物理行只能包含一个紧凑 JSON 对象，所有阶段最后一行必须为 `{"type":"end"}`。middle Prompt 只承载项目背景、文体、翻译习惯、术语偏好、校对或润色标准。

四阶段分别读取自己的：

```toml
enabled = true
previous_segments = 3
```

数量表示当前 `(file_id, part_id)` 中、当前 Chunk 首个 Segment 之前最近的非空
Segment 数。

规则：

- 上文不得跨 `file_id` 或 `part_id`。
- 一个 Chunk 共享一份 `reference_context`。
- `reference_context` 只携带理解所需的源文和可用目标文本，不携带 Segment ID。
- 上文不属于本次进度范围，不要求 LLM 输出。
- 失败或未完成 Segment 的源文仍可作为上文。
- 失败结果不能作为可信目标文本上文。
- 术语上文始终只含源文。

翻译、校对和润色请求把当前请求内的待处理 Segment 依次编号为 `"1"`、`"2"`……；模型只返回这些短 ID。宿主在解析后映射回持久 Segment ID，未知、重复或缺失短 ID 仍按格式修正规则处理。格式修正、校验修复、上下文拆分和超长 Segment part 请求各自重新编号。术语请求不发送 Segment ID。

诊断详情只在本次运行的有界内存中保留短 ID 到持久 Segment ID 的映射；摘要接口不传输该映射。项目数据、阶段结果、普通日志和 debug manifest 继续只使用持久 Segment ID。

`ordered_by_file`：

- 同一 `(file_id, part_id)` 内 Chunk 顺序执行。
- 不同文件可并发。
- 翻译、校对和润色上文可包含此前已有的阶段文本。

`parallel`：

- 所有 Chunk 可并发。
- 上文只含源文，不依赖当前 Run 尚未产生的结果。

术语提示词必须说明：参考上文只用于判断人物性别、指代、身份和上下文含义，不要仅因词语只出现在上文中就提取。

## 4.2 术语

### 发布库与活动任务

术语目录只维护：

- 一份存放在 SQLite 中的已发布术语库。
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
不会自动删除。术语“移除”通过人工 disabled override 完成；Web 还提供“彻底删除”，
会同时删除术语记录及对应 disabled override，使后续扫描可以重新发现该术语。彻底
删除不删除历史 scan/candidate 记录。旧任务记录可以留在追加文件中，但不再参与产品
逻辑，也不提供历史代次管理功能。

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

LLM 不需要声明术语属于哪个 Segment。合法术语行可以先保存为候选；只有所有行合法且最终存在 end 时，请求覆盖的每个 Segment 才记录扫描 completed。否则格式修正仍重试原请求范围。`end` 必须严格等于 `{"type":"end"}`；例如 `{"type":"type":"end"}` 仍拒绝，不自动修复或接受。严格失败不会回滚已经解析的候选，失败 Segment 会记录安全错误分类，Run manifest 记录分类及数量。

活动扫描的合法候选可以在全量扫描完成前读取：`terms-export --source scanned`
或 Web 术语页的“导出当前扫描结果”只导出当前活动任务候选，不改动已发布库。用户
确认 `terms-publish-partial` 或 Web 的“发布现有结果”后，候选按现有去重、冲突和
override 规则写入 SQLite 中的普通术语库，不添加 partial 标记，立即可供翻译、校对和润色。
该操作只把当前活动扫描标记为 `partial_published`，保留 scans、candidates 和历史
Run；下一次扫描创建新的活动任务，不删除旧记录。

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
    {"source": "...", "translation": "..."}
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
    {"id": "1", "source": "..."}
  ]
}
```

输出 JSONL：

```jsonl
{"type":"segment","id":"1","translation":"..."}
{"type":"end"}
```

映射规则：

- 只接受本次请求范围内从 `"1"` 开始的短 ID；宿主负责映射回持久 Segment ID。
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
2. 同一 `file_id + part_id` 内按源文顺序分组；中间只有空行时仍可组成一个修复 Chunk。
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
{"type":"segment","id":"1","status":"accepted"}
{"type":"segment","id":"2","status":"suggested","suggested_text":"完整建议译文","reason":"遗漏否定含义"}
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
- 不覆盖翻译或建议历史，写入 SQLite 中独立的 applied 记录。

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

每次尝试加入 Segment 后重新渲染并估算完整 Prompt。普通 Run 创建后才规划下一批 Chunk，调度器只保留与 `max_parallel` 同阶的有界缓冲；Chunk ID 在进入调度时生成，调试模式随生成追加 Chunk Manifest。取消后不再继续规划。Chunk 参数只影响本次请求组合，不影响任何已完成 Segment。

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
`/choices/0/message/content`。另内置 `anthropic`、`google-gemini` 与
`openai-responses` 定义：分别使用 `messages_format` 消息形状转换、Preset
`endpoint` 的 `${model}` 占位符与 `/output/-1/content/-1/text` 响应路径。
声明式 Adapter 只支持非流式 JSON POST。

Adapter 可声明可选的 `models` 规格与 `usage` 映射。`models` 由 Web 在用户
手动触发时以非流式 GET 检测连通性并读取模型列表，用于填写 Preset；不自动
判断 Provider 或切换端点。探测使用当前 Preset 草稿并严格校验，但不保存草稿。
模型 ID 始终允许手工输入；发现结果只在当前列表中搜索，选择后仍需显式保存。
`usage` 把端点响应中的消耗换算为 input/output/
total 规范化计数，宿主在任务内累计端点实际返回的消耗，写入任务摘要与 Run
`manifest.json`；任一成功响应未返回完整 usage 时，整个任务明确显示不可用且
公开计数归零，不使用本地估算。同一 Run 的续作累加各次精确回报；缺少累计
版本标记的旧 Run 或任一次回报不完整时，整个 Run usage 不可用。

项目的全局 `llm.preset` 及四个可选阶段覆盖实时解析全局命名 Preset。每个阶段
只解析自己的覆盖或全局默认，不增加其他继承层。Preset 提供 Adapter ID、URL、
模型、API Key 环境变量名、代理、Token 能力、限速、并发、超时和
`extra_body`。`extra_body` 必须是 JSON 对象，可以包含嵌套对象和数组；宿主在
Adapter 完整 body 渲染后追加其顶层字段。任何顶层字段冲突、模板占位符或缺失
Adapter 都在创建 Run 或发送请求前失败，不覆盖、不递归合并、不自动 fallback。
`run-all` 对相同 Preset 复用 HTTP Client 和限速窗口，对不同 Preset 分别维护；
Run 快照与阶段指纹始终记录当前阶段实际使用的 Preset 和 Adapter。
Web 请求预览显示最终 body，并以 `***` 脱敏认证 Header。Preset 内容进入 Run
快照和阶段指纹，因此其中不得保存密钥。项目配置、全局配置和 Run 快照都必须
包含 `llm.preset`；内联连接字段和缺失 `llm_preset.json` 的 Run 续作直接失败。

整个命令共享一个 `httpx.AsyncClient`：

- connect、read、write 和 pool timeout 都使用 `request_timeout_seconds`。
- 连接池上限从 `max_parallel` 派生。
- `asyncio.Semaphore` 控制并发。
- 显式代理使用 `proxy=proxy_url`；空值不关闭 HTTPX 的标准环境代理。

RPM 和 ITPM 使用单进程 60 秒滑动窗口。检查与预约由同一个异步锁保护。每次实际 HTTP 尝试都重新预约额度，失败后不返还。RPM 大于 0 时，实际尝试还按 `60 / RPM` 的最小间隔串行预约发起许可；首个尝试立即预约，HTTP 请求发出后释放该许可，因而不会在启动时突发。RPM 为 0 时不启用该节奏；ITPM 为 0 时不参与 Token 窗口；两者都为 0 时仍受 `max_parallel` 限制。

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
- Adapter 还可配置 `response_reasoning_content_pointer` 提取字符串或 null 的
  结构化思考字段。规范化响应包含 `content` 和可空的 `reasoning_content`；
  结构化字段与内嵌块同时非空时快速失败，不猜测合并顺序。
- 思考正文只存在于当前请求生命周期，不属于 Prompt、Chunk、Segment 结果或
  进度。普通模式不持久化；debug 模式仍只在原始响应 Payload 中保存，不新增
  独立思考记录。
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

项目内进度记录使用 `project.sqlite` 的事务、外键、唯一约束和 WAL。项目数据库
包含明确的 `schema_version`；缺失或未知版本快速失败并提示重新创建项目，不提供
JSONL 到 SQLite 的迁移、双写或旧格式读取。Run 的可读 `manifest.json`、配置和
Prompt/Preset/Adapter 快照仍保存在对应 Run 目录，数据库中的 Run 索引负责活动任务
发现和恢复判断。

项目外的普通 JSON（全局配置、Preset、导出交换文件等）使用同目录临时文件写完后
原子替换。`--dry-run` 不写入项目数据库，以保持零写入。

恢复只读取数据库中的：

- 每个 Segment 是否存在 completed。
- 没有 completed 时最近是否 failed。
- 活动术语任务中的 Segment scan 状态。

Segment 进度恢复不读取 Chunk ID、Chunk Manifest 或 Request 状态。Run 状态
只用于发现可续作的执行身份和旧 scope；实际 pending 仍完全由 Segment 结果或
术语 scan 推导。

## 5.5 普通日志与调试模式

普通模式保存：

- Run Manifest 和快照。
- SQLite 中的 Segment 阶段结果、术语任务进度与候选。
- 人类可读 `app.log`。

CLI 无论 debug 是否启用都将带时间、级别和阶段的实时日志写入 stderr，并把相同基本日志写入 `app.log`；最终命令汇总单独以 JSON 写入 stdout。日志不得包含正文、译文、Prompt、API Key 或完整 Payload。

本地 Web 将相同安全摘要写入应用级 `logs/app.log`，按大小轮转，不因项目切换
而清空；同时在有界内存中保留当前进程的结构化日志供仪表盘读取。仪表盘还为
当前 Run 保留最近 50 个逻辑 LLM 请求；HTTP 重试只追加到同一请求。每项保存
规范化 messages、解析后的 Content 和 Reasoning、模型、状态、HTTP 尝试次数、
状态码与延迟。单条 message 和 Content 最多 100,000 字符，Reasoning 最多
20,000 字符，超限时在详情中明确标记截断。

这些请求详情可能包含 Prompt 和源文，只能通过当前进程的详情接口按请求 ID
读取；诊断摘要轮询不返回正文。新 Run 开始时清空，应用重启后丢失，不经过
普通 logger，也不会额外写入轮转日志、Run 文件或项目数据。内存详情不采集
Header、API Key、Adapter Wire Body 或 Provider 原始 REST JSON；解析失败和
终止错误只记录安全错误类别。显式启用 debug 时既有调试 Payload 仍按下述规则
独立保存，不复用仪表盘的内存详情。

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
python -m app.main init --empty --name PROJECT_NAME
python -m app.main init --empty --name PROJECT_NAME --parent-dir PARENT
python -m app.main files-add PROJECT INPUT...
python -m app.main files-remove PROJECT FILE_ID...
python -m app.main inspect PROJECT
python -m app.main terminology PROJECT
python -m app.main terms-import PROJECT terms.json
python -m app.main terms-export PROJECT terms.csv
python -m app.main terms-export PROJECT scanned-terms.json --source scanned
python -m app.main terms-publish-partial PROJECT
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
--empty
--parent-dir
```

语义：

- `--force`：重做选定范围内所有非空 Segment，覆盖普通 pending/failed 筛选。
- `--dry-run`：不写文件、不创建 Run、不更新模板、不调用 LLM；它会耗尽规划器，报告范围、设置警告、完整 Chunk 数和 Token 估算。普通 Run 不在启动前生成全部 Chunk。
- `--allow-outdated-base`：仅用于 apply，允许应用基于旧上游结果的建议。
- `--allow-missing`：仅用于 export，允许使用阶段回退。
- `--bilingual`：仅用于 export。
- `--all`：apply 的必需批量确认。
- `terms-import` 根据 `.json` 或 `.csv` 扩展名增量导入术语；`--dry-run`
  只校验并报告变化。
- `terms-export` 根据输出扩展名导出术语；默认不含 disabled，
  `--include-disabled` 用于完整备份人工移除决定；`--source scanned` 导出当前活动
  扫描中已经解析的候选。
- `terms-publish-partial` 在当前术语扫描未运行且存在候选时显式发布部分结果；Web
  端要求同样的确认，不提供自动发布或自动修复路径。
- `--resume-run`：仅用于四个独立 LLM 阶段，续用最近同阶段 running Run。
- `--decline-run`：仅用于四个独立 LLM 阶段，明确结束该候选并创建新 Run。
- `--reuse-mixed-fingerprints`：显式复用选定范围内设置指纹不同的 completed；
  仅用于四个 LLM 阶段和 `run-all`，并与 `--force` 互斥。
- `--document-adapter`：用于带输入的 init 或 files-add，显式选择输入 Adapter；
  init 默认 `txt`，空项目不保存该选择。
- `--empty`：仅用于 init，显式创建 0 文件项目，不能同时提供输入。
- `--parent-dir`：仅用于 init，在指定的现有可写父目录创建项目；省略时使用
  内置 `projects/`。
- `files-add`：追加源文件；省略 Adapter 时按已安装 Adapter 的扩展名识别，
  目录使用 `--recursive` 递归发现所有受支持格式。显式选择 Adapter 时由该
  Adapter 解释输入。
- `files-remove`：按 File ID 从活动项目移除文件，不清理历史结果。
- `export --file FILE_ID`：只导出指定活动 File；参数可重复。省略时导出全部
  活动 File，未知、重复或显式空范围在发布前失败。

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
- 活动术语任务的候选数量、失败分类和可导出的部分结果；严格 JSONL 失败不会丢弃
  已解析候选，部分发布后下游立即使用普通术语库。
- 翻译、校对、润色和 applied 的 completed、failed、pending。
- 当前设置指纹与已有结果指纹的差异及混合来源数量。
- 有旧 completed 但最近重做失败的 Segment 数。
- validation warning 数。
- 基于旧上游的校对或润色建议数。
- 当前仍为 running 的 Run ID、阶段、开始时间和原始范围。
- 是否存在新的全局模板。
- 建议的下一条命令。

inspect 不修改 Run 状态或用户进度。旧 JSONL 项目不做尾行修复，直接提示重新创建。

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
- 默认逐 File 使用其来源 Document Adapter；`--format txt` 改用宿主内置 TXT
  导出。插件缺失、版本不兼容、状态损坏或能力不足时明确失败，不静默转换。
- 所有 Adapter 先在同一宿主临时目录生成并完成路径校验，再移动到正式目录；
  任一生成或校验失败时不发布输出。

TXT 按 `file_order` 和 `line_index` 重建，每个输入文件独立导出，并使用
`project.output_encoding` 严格编码。编码无法表示结果字符时失败，不静默替换。
EPUB 输出一个 `.translated.epub` 或 `.bilingual.epub`，只重写翻译对应的
XHTML 文本单元及其定位槽位。普通复合 Segment 的单语译文写入首个槽并清空
其余槽，保留原内联标签骨架；包含 Ruby 的复合 Segment 可以混合普通槽和 Ruby
槽，单语移除该 Segment 的全部 Ruby，双语在完整源句末尾追加译文。只有旧的
独立 Ruby locator 继续按其专用定位规则导出。

```bash
python -m app.main export PROJECT --stage translated --format original
python -m app.main export PROJECT --stage translated --format txt
python -m app.main export PROJECT --stage translated --file F0001 --file F0003
```

除各 File 来源格式和 TXT 外，不提供任意格式转换。
文件范围同时适用于原格式和 TXT；只校验所选 File 的阶段结果。宿主保持
`file_order`，不提供按 Segment 导出或跨 File 合并。

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
本机 Host 和跨站 Origin。Web 创建项目时不预选文档格式；待输入列表可分多次
加入单独文件或文件夹，并可在同一项目中混合 Adapter。文件夹输入保留内部
相对路径，单独文件只保留 basename；大小写不敏感的重名使本次选择整体拒绝。
文件夹内不支持的文件被忽略并汇总提示，单独选择不支持文件直接失败。项目
概览使用同一输入队列追加文件，并可经典多选移除。

保存父目录和打开项目目录默认填入服务端绝对 `projects` 路径，用户可直接修改。
不支持 `webkitdirectory` 的浏览器明确禁用文件夹选择，但仍可选择单独文件。
外部项目使用项目自身 ID 作为 Web 路由标识；路径规范化并去重，无效项目、
不可写父目录和目标冲突在写入前失败。

Web 只在当前浏览器的版本化 localStorage 保存最近外部项目路径。页面加载时
逐一向本机服务提交这些精确路径；不扫描父目录，不自动移动项目。失效路径会
明确提示并从最近列表移除，默认 `projects/` 项目继续直接列出。导出页用
Ctrl/Cmd/Shift 经典多选限定文件范围，未选择时导出全部。项目配置使用覆盖
全部现有字段的分组表单；
项目 Prompt 与 JSON LLM Adapter 在设置页分别提供高级编辑器。Web 还提供全局配置、
全局 Prompt 和 LLM Preset 管理；全局配置与 Prompt 只影响新项目或用户明确
同步的项目，Preset 修改则立即影响引用项目。Web 还可运行/取消阶段任务、人工
审校、apply 和 export。

Web 与 CLI 使用同一 SQLite 项目数据库、应用内核和持久化记录。同一项目的写任务
通过非阻塞文件锁互斥，冲突时明确失败。后台任务状态只存在于当前 Web 进程，重启
后仍由项目 Run 与 Segment 记录恢复业务进度。

Web 顶部的全局任务状态条在项目概览、导出、设置和窄屏布局中持续显示最近
任务，直到下一任务开始。状态条提供运行项目、阶段、状态、已完成/失败/待处理
与总 Segment、三段式进度、当前 Run 累计输入与输出 Tokens，以及运行中的取消
入口；复用 Segment 计入已完成，不显示 Combined Tokens。失败数量可点击跳转到
当前阶段的错误筛选；错误行只显示稳定的安全错误分类和摘要。完成、失败和取消
状态只保留在当前页面会话，刷新后不从已结束 Run 重建。

Web 仪表盘不依赖当前项目，可查看全局日志和当前运行的请求并发数、请求延迟、
HTTP 错误、重试、当前限流等待请求数、累计输入/输出 Tokens 与总吞吐量。当前
等待数包含本地 RPM/ITPM 额度、RPM 发起许可排队以及 HTTP 429 的 Retry-After
或退避；网络错误、408 和 5xx 的普通重试退避不计入。日志支持级别、
项目、阶段和文本过滤，并可暂停自动滚动。吞吐量只使用完整端点 usage 除以当前
运行耗时；usage 缺失或不完整时，Token 计数和吞吐量均显示不可用。右侧请求/
响应列表只轮询轻量摘要；双击请求或选择“查看”后，按需显示请求、Content、
Reasoning 和尝试详情，运行中的已打开详情随仪表盘刷新。完整详情可能包含 Prompt
和源文，但普通日志和指标仍不记录鉴权 Header、请求/响应 Payload、Prompt 或
源文。

0 个非空 Segment 时，阶段运行入口不可用，服务端预检也拒绝创建后台 WebTask。
文件移除确认必须说明源副本和活动 Segment 会删除，而历史阶段结果及既有输出
保留，重新添加会获得新 ID。

Web 只在术语、翻译、校对和润色页面提供阶段启动入口。每次启动前读取当前阶段
统计和最近的 running Run：存在未完成 Run 时必须明确续用或结束后新建；存在
不同设置指纹的 completed 时必须明确复用或 force。force 与复用互斥，续用 Run
时不接受会被忽略的 force 或复用参数。所有决策在创建后台任务和阶段 Run 前
再次校验；并发修改导致条件变化时明确失败，不自动降级。桌面和窄屏使用同一
运行决策流程。

术语页支持 JSON/CSV 导入导出、经典 Ctrl/Cmd/Shift 多选和批量移除；活动扫描
期间显示完成/失败/待处理、失败分类和候选数量，并可导出当前候选或在确认后部分
发布。部分发布立即更新 SQLite 中的普通术语库，只结束活动扫描状态，保留历史扫描和
候选记录，下一次扫描使用新的任务。翻译、校对、润色列表使用相同多选规则；批量
清除采用追加 reset。校对和润色可应用
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

- 文件和 Segment 顺序正确，不跨 File 或文档 part 混合。
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

- 四阶段上文数量分别生效且不跨 `file_id` 或 `part_id`。
- 术语上文只含源文，不计入扫描进度。
- ordered_by_file 和 parallel 的上文内容符合定义。
- 术语按 normalized source 合并，override 和 disabled 生效。
- 活动术语任务可续作，force 新任务完成前不替换已发布库。
- 失败扫描保留候选与安全错误摘要；当前候选可导出，显式部分发布只清除活动扫描
  状态并保留历史记录。
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
- 结构化思考 Pointer 的字符串/null、缺失路径、非法类型及其与内嵌思考块冲突
  均按规范化边界处理；普通模式不新增思考持久化记录。
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
- LLM Preset 的实时解析、嵌套 `extra_body`、顶层冲突和占位符拒绝生效；实际
  Preset 快照与内容 Hash 进入 Run 和阶段指纹，请求预览不泄露认证 Header。
- 四阶段可分别覆盖全局 Preset；相同 Preset 在 `run-all` 中共享 HTTP Client
  和限速窗口，不同 Preset 使用独立资源，Run 与指纹记录实际阶段 Preset。
- TXT 旧项目没有 Document Adapter 字段时仍按 `txt` 导出。
- EPUB 保持 spine 顺序、跨节点 Segment 定位、导航、元数据和非翻译资源；
  纯译文和双语文件均可重新打开。
- EPUB Ruby 与同一文本流的前后文合为语义 Segment，三种导入模式、纯译文移除
  全部 Ruby 和双语在完整源句末尾追加译文均生效；导入选项只固化在对应 File
  Adapter 状态。
- EPUB ZIP 路径、符号链接、压缩炸弹、非法版本化 DOCTYPE 和 XML 实体输入明确
  拒绝；普通内联文本合并、混合 Ruby 复合定位和旧独立 Ruby locator 均保持可导出。
- Document Adapter 缺失、版本不兼容、状态损坏或运行失败时不发布部分输出，
  也不静默回退。
- Python 插件发现拒绝重复 ID 和未知协议版本。
- Document Adapter 扩展名按大小写不敏感保持唯一；Web 待输入列表支持混合
  文件、文件夹相对路径、批次冲突阻止和不支持文件汇总。
- Web 只接受本机 Host/Origin，与 CLI 共用项目记录；同项目第二个写任务明确
  失败，取消后的 Run 有正确收尾。
- Web 项目配置表单覆盖完整配置 schema；非法类型或组合不改变原 TOML，保存
  后的规范 TOML 可由 CLI 原样读取。
- Web 可在没有打开项目时管理全局配置、Prompt 和 Preset；全局模板修改不改变
  现有项目，显式同步通过严格校验；内联 LLM 配置在读取边界直接拒绝。
- Web 仪表盘在项目切换和窄屏下可见全局日志、当前请求指标及精确 usage；过滤、
  搜索和自动滚动暂停生效，usage 不完整时不显示估算吞吐量；本次运行请求/
  响应详情可按需查看且不会由仪表盘写入磁盘。
- CLI 可在指定父目录创建并通过绝对路径打开项目；Web 可创建、打开、去重并
  恢复最近外部项目，且不会扫描父目录或移动项目。
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
