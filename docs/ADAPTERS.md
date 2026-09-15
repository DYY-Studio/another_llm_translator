# Adapter 契约

本文是 LLM Adapter、Document Adapter、可信 Python 插件和 LLM Preset 协议的唯一权威文档。
协议字段、版本、能力、错误边界和兼容范围只在此定义；测试用于验证本契约，不能以其他文档
中的重复描述覆盖它。

稳定产品语义见[最小产品规范](MINIMAL.md)，用户配置和操作见[用户指南](USER_GUIDE.md)，
尚未实现的协议方向与稳定门槛见[产品路线图](ROADMAP.md)。

## 1. 声明式 JSON LLM Adapter（已实现）

全局定义位于 `llm_adapters/<adapter_id>.json`。LLM Preset 引用 Adapter ID；
项目实时引用全局 Adapter，不保存项目副本。每个 Run 保存实际使用的 Adapter
与 Preset 快照。

最小定义：

```json
{
  "schema_version": 3,
  "adapter_id": "openai-compatible",
  "endpoint": "/chat/completions",
  "headers": {
    "Authorization": "Bearer ${api_key}"
  },
  "body": {
    "model": "${model}",
    "messages": "${messages}",
    "temperature": "${temperature}",
    "max_tokens": "${max_output_tokens}",
    "stream": "${stream}"
  },
  "response_content_pointer": "/choices/0/message/content"
}
```

需要读取端点提供的结构化思考正文时可增加：

```json
{
  "response_reasoning_content_pointers": [
    "/choices/0/message/reasoning_content",
    "/choices/0/message/reasoning"
  ]
}
```

该字段可省略；内置 OpenAI-compatible 定义按上述顺序兼容两种字段。已有配置仍可
使用单数的 `response_reasoning_content_pointer`，但单数与复数不能同时配置。候选
路径只在前一路径缺失时继续尝试；首个存在的 `null` 表示没有 reasoning，多个字段
不会拼接。

需要把规范化的 system/user/assistant 消息转换为 Provider 原生形状时，可设置
`messages_format`（可选，默认 `openai` 原样透传）：

- `anthropic`：system 消息剥离到顶层字段，其余 user/assistant 消息保留
  字符串 content；
- `gemini`：system 消息剥离到顶层字段，user/assistant 映射为 `contents`
  数组的 `role: "user"/"model"` 与 `parts: [{"text": ...}]`。

`body` 模板可使用 `${system}` 占位符读取剥离后的 system 文本（多条消息以空行拼接）。
例如，Anthropic 使用顶层 `system`，Gemini 使用 `system_instruction`。

该占位符对 `openai` 格式同样可渲染，但 system 消息仍保留在 `${messages}` 中，
通常不应同时使用。

### 请求边界

- 请求地址只由当前 Preset 的 `base_url` 与 Adapter 的 `endpoint` 组成。API 版本前缀
  （如 `/v1`、`/v1beta`）写入 `base_url`，Adapter `endpoint` 必须是相对路径。
- Adapter `endpoint` 允许且只允许 `${model}` 占位符（如 Gemini 的
  `/models/${model}:generateContent`），请求时由宿主替换为模型名；
  其他占位符立即失败。
- 未声明 `streaming` 的 Adapter 只支持普通 JSON POST；声明后可由 Preset 显式
  启用 SSE。HTTP Client、代理、超时、限速、重试、取消和日志始终由宿主负责。
- `body` 是完整模板。可直接加入 `reasoning_effort`、`response_format` 或
  Provider 自定义嵌套字段，不存在与宿主默认字段合并的覆盖顺序。
- body 支持 `${model}`、`${system}`、`${messages}`、`${temperature}`、
  `${max_output_tokens}`、`${stream}`。占位符必须独占一个 JSON 字符串值，
  替换后保留数组、数字和布尔类型。
- 当 Preset 的 `max_output_tokens` 为 `0` 时，值为 `${max_output_tokens}` 的
  body 对象字段会被省略；自定义常量字段保持原样。
- Header 可使用上述占位符及 `${api_key}`；嵌入字符串时结果为字符串。
- 未知字段、未知占位符、混合 body 文本占位符和非法 schema 立即失败。

> [!CAUTION]
> `${api_key}` 禁止出现在 body。URL 不支持模板，因此密钥也不能进入 URL。

### SSE 流式规则（可选）

schema 3 的 Adapter 可以增加 `streaming` 对象；宿主在全局 Adapter 摘要中以
`streaming_supported` 报告该能力。当前唯一支持的传输是 SSE：

```json
{
  "streaming": {
    "transport": "sse",
    "endpoint": "/chat/completions",
    "request_body": {"stream_options": {"include_usage": true}},
    "content_events": [
      {"pointer": "/choices/0/delta/content"}
    ],
    "reasoning_events": [
      {"pointer": "/choices/0/delta/reasoning_content"},
      {"pointer": "/choices/0/delta/reasoning"}
    ],
    "terminal": {"sentinel": "[DONE]"},
    "allow_clean_eof": false,
    "error_events": [
      {
        "when": {"pointer": "/choices/0/finish_reason", "equals": "error"},
        "message_pointer": "/error/message",
        "status_pointer": "/error/status"
      }
    ],
    "usage": {
      "input_tokens_pointers": ["/usage/prompt_tokens"],
      "output_tokens_pointers": ["/usage/completion_tokens"],
      "total_tokens_pointers": ["/usage/total_tokens"]
    }
  }
}
```

`streaming.endpoint` 是启用流式请求时使用的必填相对路径，与顶层 `endpoint` 使用相同
占位符规则；即使两种请求使用同一路径也必须显式声明。`request_body` 只在流式请求加入，
不能与基础 `body` 或 Preset `extra_body` 的顶层字段冲突。

`content_events` 必须非空；每项以 `pointer` 指向字符串增量，可选 `when` 条件为 `{"pointer": "...", "equals": <primitive>}` 或 `{"pointer": "...", "exists": true}`。条件匹配后路径缺失或类型错误立即失败，未知事件忽略。

`reasoning_events` 可以为空；内置 Anthropic 不暴露 thinking。OpenAI-compatible 可同时声明 `delta.reasoning_content` 与 `delta.reasoning`，以兼容两类互斥的流式字段。

`terminal` 二选一声明 sentinel 或条件；默认每条流必须命中终止条件。`allow_clean_eof` 是可选布尔值，缺省为 `false`。

显式启用后，HTTP 2xx 响应在已收到至少一个合法 SSE `data` 事件、没有读取/协议错误并自然到达 body EOF 时，也可作为第二种终止方式；EOF 前的所有事件仍会被读取，因此尾部 usage 能够被收集。这只放宽传输终止判定，聚合正文仍须通过现有响应格式解析和阶段校验。

读取超时、网络中断、SSE/UTF-8/JSON 错误和流内服务错误不属于 clean EOF，仍按原规则失败或重试。`error_events` 声明流内错误、可选字符串消息路径和可选 `status_pointer`。

状态路径命中后必须是 100–599 的整数；它表示 Provider 在 SSE 事件中报告的上游状态，不覆盖实际 HTTP 响应的 `http_status`。例如 OpenAI-compatible 的 `finish_reason=error` 可以同时报告外层 HTTP 200 和上游 HTTP 504。

`usage` 的三个候选指针数组逐事件观察，每个指标保留最后一个非负整数，只有声明的全部指标都取得时 usage 才可用。

宿主严格处理 UTF-8、CRLF/LF、chunk 边界、注释和多行 `data:`，在未满足显式终止或已声明的 clean EOF 前断流、或遇到流内服务错误时丢弃完整聚合正文，并按既有 HTTP 尝试次数重试；不会隐式改为非流式。

普通日志和诊断摘要不包含增量正文，debug 模式才保存原始 SSE `data` 事件。流式 timeout 是连接及连续读取的空闲超时，不限制完整生成时间。

内置 `openai-compatible` 同时接受 `[DONE]` 和显式启用的 clean EOF；Responses、Gemini、Anthropic 仍要求各自声明的终止事件。宿主不识别 `cost` 或其他供应商字段作为隐式终止标记。

### 响应边界

`response_content_pointer` 是必需的 RFC 6901 JSON Pointer，结果必须是字符串。JSON Pointer 的数组索引 token 支持负索引 `-N`（RFC 6901 扩展）：`-1` 为最后一个元素、`-2` 为倒数第二。

当思考块总是排在最前、文本块在最后时 （Anthropic `content`、Gemini `parts`），负索引可稳定取到最后文本块。越界、空数组与普通缺失路径同样快速失败。可选的 `response_reasoning_content_pointer` 结果必须是字符串或 null。

也可使用非空的 `response_reasoning_content_pointers` 数组声明有序候选路径：路径缺失时继续尝试，首个存在的 `null` 规范化为 null，字段存在但类型错误时当前请求失败，不猜测或拼接多个字段。

Adapter 规范化返回 `content` 和可空的 `reasoning_content`。宿主随后按统一严格规则，
从 content 开头剥离一个完整已知思考 Tag。若结构化字段与内嵌块同时非空则快速失败，
不猜测拼接顺序。

`reasoning_content` 只存在于当前请求生命周期，不进入阶段记录。debug 模式只保留原始响应，
不新增思考副本。

常规 HTTP 状态与网络异常不由 Adapter 分类。需要特殊签名、非 JSON body、
非 JSON 成功响应或特殊错误解析的端点超出当前声明式 schema 范围。

### 指纹与密钥

阶段指纹包含 Adapter ID、全局定义内容 Hash、Preset ID 和 Preset 内容 Hash。
Run 快照保存定义原文，但不解析或保存 credential 引用所指向的 API Key。调试请求记录
仍经过敏感 Header 清理。

### 模型发现与 usage 映射

Adapter 可声明可选的 `models` 规格。宿主只在用户手动触发时执行连通性检测
并读取模型列表，用于填写 Preset；不自动判断 Provider、选择模型或切换端点：

```json
{
  "models": {
    "endpoint": "/models",
    "response_models_pointer": "/data",
    "response_model_id": "id",
    "response_model_display": "display_name",
    "response_model_strip_prefix": "models/"
  }
}
```

- models 的 `endpoint` 与主请求一样是不含版本前缀的相对路径；版本前缀由
  Preset `base_url` 提供（如 `https://api.anthropic.com/v1`）。
- models 请求固定为非流式 GET，URL 由 Preset `base_url` 与 `endpoint`
  组成，Header 复用顶层 `headers` 模板；渲染时只提供 `${api_key}`，
  含其他占位符的 Header 在触发模型发现时明确失败。
- Web 触发模型发现时提交当前 Preset 草稿并执行与保存相同的严格校验；探测
  使用草稿中的 Adapter、Base URL、credential 引用、代理和超时，并按必填的
  `key_index`（从 1 开始）只使用所选 Key，但不保存草稿。路径 Preset ID 与草稿
  ID 不一致时立即拒绝；Key 缺失、格式错误或序号越界均立即报告。
- Preset 编辑器保留可手工输入的模型 ID；发现结果在字段下方按名称或 ID
  本地搜索。选择结果只更新当前草稿并收起列表，必须显式保存才会生效。
- `response_models_pointer` 指向模型条目数组；`response_model_id` 是条目内
  必填键名；`response_model_display` 与 `response_model_strip_prefix` 可选。
- 展示名缺失时回退为模型 ID；`response_model_strip_prefix` 从模型 ID 前缀
  剥离（如 Gemini 的 `models/`）。条目缺少 ID 或响应形状非法时快速失败。
- 缺少 `models` 规格、缺失或格式错误的 credential、HTTP 错误或网络异常都明确
  报告，不猜测、不 fallback。请求使用 Preset 的代理与超时设置。

Adapter 可声明可选的 `usage` 映射，把端点响应中的消耗换算为规范化计数：

```json
{
  "usage": {
    "input_tokens_pointer": "/usage/prompt_tokens",
    "output_tokens_pointer": "/usage/completion_tokens",
    "total_tokens_pointer": "/usage/total_tokens"
  }
}
```

- 三个指针均可选；任一成功或失败尝试的已声明指针缺失或值非法（非非负整数）时，
  任务标记为 partial；保留已观测计数但吞吐量不可用，不使用本地启发式估算冒充
  端点账单。完全没有可观测 usage 时显示 unavailable。
- 宿主在每个尝试收到 usage 后累计规范化计数，任务结束时写入任务摘要与 Run
  `manifest.json` 的 `usage` 字段。Adapter 未声明 `usage` 时任务与 Run
  摘要不包含 usage 字段。
- 主请求的限速、重试与错误语义不受影响。

### 内置 Adapter 定义

- `openai-compatible`：
  - 使用 Bearer API Key 和 Chat Completions body。
  - 正文 pointer 为 `/choices/0/message/content`。
  - 推理 pointers 为 `/choices/0/message/reasoning_content` 和
    `/choices/0/message/reasoning`。
  - SSE 推理增量对应 `/choices/0/delta/reasoning_content` 和
    `/choices/0/delta/reasoning`。
- `anthropic`：`x-api-key` 与 `anthropic-version: 2023-06-01`，body 顶层 `system`，pointer `/content/-1/text`。

  未启用 thinking 时 content 首块即文本；负索引使 `extra_body` 日后启用 thinking 时仍可稳定取到最后文本块。不配置 reasoning 指针；需要思考正文时可复制定义并设 `/content/-2/thinking`。

  未启用 thinking 或思考块缺失时结果为 null；字段存在但不是字符串或 null 时快速失败。
- `google-gemini`：`x-goog-api-key`（密钥不进入 URL），model 由 Adapter `endpoint` 的 `${model}` 占位符进入请求路径，pointer `/candidates/0/content/parts/-1/text`。

  不内置 thinkingConfig，思考模型默认思考开启时 text 块仍恒为最后一个 part。不配置 reasoning 指针；可自配 `/candidates/0/content/parts/-2/text`，缺失路径结果为 null，字段存在但不是字符串或 null 时快速失败。
- `openai-responses`：`input` 原样接收规范化消息（system/user/assistant），body 含 `"store": false`，pointer `/output/-1/content/-1/text`。

  宿主直接解析 REST JSON，不使用 SDK 才提供的 `output_text` 便利属性；当前请求不声明 tool，因此最终 message 是最后一个 output，正文是其最后一个 content。响应缺少该结构时快速失败。

四个内置定义都声明 `streaming`、`models` 与 `usage` 映射；示例 Preset 见 `llm_presets/anthropic-claude.json`、`google-gemini.json` 与 `openai-responses.json`。

Anthropic 无 total 计数，Gemini 的模型 ID 经 `models/` 前缀剥离。所有内置 Adapter 的主请求、流式和 `models` 端点都不含版本前缀；版本前缀（`/v1`、`/v1beta`）必须写在 Preset `base_url` 中。

## 2. Document Adapter（Beta）

Document Adapter 是同一格式的导入与导出边界。当前内置 `txt` 与 `epub`；独立发行的
`another-llm-translator-srt` 插件提供 `srt` Adapter：

```python
class DocumentAdapter(Protocol):
    adapter_id: str
    version: str
    capabilities: frozenset[str]
    extensions: frozenset[str]
    import_options: tuple[DocumentChoiceOption, ...]
    run_options: tuple[DocumentChoiceOption, ...]

    def model_prompt_requirements(
        *, stage: str, language: str, opaque_state: dict | None,
        run_options: dict[str, str]
    ) -> str | None: ...

    def render_model_source(
        *, segment: dict, opaque_state: dict | None,
        run_options: dict[str, str]
    ) -> str: ...

    def normalize_model_output(
        *, segment: dict, text: str, stage: str,
        opaque_state: dict | None, run_options: dict[str, str]
    ) -> str: ...

    def replacement_options(
        *, opaque_state: dict | None
    ) -> dict[str, str]: ...

    def import_sources(...) -> DocumentImport: ...
    def export_sources(...) -> list[Path]: ...
```

`export_sources` 还会收到宿主项目配置中的 `target_language: str` 和 `target_language_tag: str`。前者是供模型和人阅读的自由文本名称，后者是可选的 BCP 47 输出语言标签；两者职责分离。Adapter 可以忽略、应用到自己的格式元数据，或在标签为空时明确拒绝导出。

宿主不按 Adapter ID 推断语言行为。Document Adapter 插件协议版本为 `12`；旧协议插件会快速失败，不保留旧调用路径。

`render_model_source` 在每个阶段开始时以同一个 File 的 `opaque_state` 和冻结的 `run_options` 生成模型源文。`model_prompt_requirements` 与可选的 `normalize_model_output` 接收完全相同的快照；宿主可据此把不同要求集合拆分到不同 Chunk。要求不得包含源文、项目路径、凭据或动态用户内容。无专属格式要求时返回 `None`。

内置 TXT 与 SRT 插件没有额外的格式 Prompt 要求，返回 `None`，并原样返回模型源文。

所有基于纯文本的 Document Adapter 都可以复用宿主提供的严格字节解码 API：

```python
from app.documents import DecodedPlaintext, decode_plaintext

decoded: DecodedPlaintext = decode_plaintext(
    data,
    confidence_threshold=0.6,
    fallback_encoding="utf-8",
)
```

`DecodedPlaintext` 返回 `text`、探测到的 `encoding_detected`、实际使用的 `encoding_used`、探测置信度 `encoding_confidence` 和不可变的 `warnings`。

API 只负责字节到文本的严格解码：它处理 BOM、chardet 探测、GB2312/GBK 到 GB18030 及 ASCII 到 UTF-8 的归一化，并在首选编码失败后只尝试一次 fallback。

它不负责换行规范化、文件发现、格式解析、文件名上下文或配置校验；这些职责仍属于各自的 Document Adapter 和宿主边界。两次严格解码都失败时抛出 `ProjectError`。

能力名为 `import`、`translated_export` 和 `bilingual_export`。宿主在调用前
检查所需能力，不支持时明确失败。

可导入 Adapter 必须声明至少一个小写、带前导点的扩展名。宿主按大小写不敏感
匹配扩展名；不同 Adapter 声明同一扩展名时插件加载直接失败，不猜测格式。
内置 TXT 声明 `.txt`、`.text`，EPUB 声明 `.epub`；SRT 插件声明 `.srt`。

### 导入

Adapter 返回有序 `ImportedFile`，每项包含原始文件位置、展示名称、Segment
净文本和输入编码信息；可选的 `model_sources` 与 Segment 一一对应，仅用于模型
看到的格式契约。宿主负责：

- 分配 File ID、Segment ID 和行序；
- 复制原始输入；
- 以临时项目目录完成事务化初始化；
- 保存通用 File/Segment 记录。

`ImportedFile` 可选返回与 `segments` 一一对应的 `segment_part_ids`。省略时宿主将所有 Segment 归入 `document`；提供时每项必须是非空字符串。

宿主把该值写入 Segment 的 `part_id`，并以 `(file_id, part_id)` 限制 Chunk、LLM 请求和参考上下文。这不会改变 File 的存储、选择、调度或导出边界；旧项目缺少有效 `part_id` 时要求重新创建，不从 locator 推测或迁移。

“内容概括 · 实验”直接复用这条通用边界契约。Adapter 不需要实现概括专用方法，也
不需要声明章节；每个 `(file_id, part_id)` 可独立生成片段概括、聚合和 Markdown 导出。
持久化 Segment 原文不随运行设置改变。运行时模型文本由 `render_model_source` 生成；宿主保存原文和实际模型文本的摘要及引用范围。当转换后的文本无法安全映射到切片时，概括请求明确失败，不猜测字符级映射。

每个 `ImportedFile` 可携带 JSON 可序列化的 `opaque_state`。宿主将其与完整的字符串 `run_options` 映射分别保存为状态记录的 `state` 与 `run_options` 字段，并在 File 记录中保存 Adapter ID、版本和状态位置；宿主只校验归属、版本和完整性，不解释 `state` 内部字段。

Adapter 可声明由固定字符串选项组成的 `import_options` 和类型相同的 `run_options`。宿主展示声明并严格校验取值；导入选项只传入 `import_sources()`，运行选项只写在 File 状态记录外层，绝不混入 `opaque_state`。

`replacement_options()` 只用 File 的 `opaque_state` 恢复当前导入选项。运行选项由宿主从状态记录外层恢复；替换时两类选项都可覆盖，未覆盖值沿用当前 File。缺失、额外、类型错误或非法值都直接失败。

仅供受控替换使用的历史选项可以声明为 `replacement_choices`；它们不会出现在新导入选项中，但会在逐 File 替换对话框中作为当前值保留并允许用户改为现行选项。

概览可逐 File 修改运行选项，也可按 Adapter 对项目内全部当前 File 部分覆盖；批量操作没有文件筛选。运行选项的修改不改写 Segment、定位状态或既有结果。`files-replace` 是受控重新导入：替换预览显示当前值，未覆盖值沿用当前 File，确认前会展示导入设置与运行设置的变化。

CLI 的 `init` 与 `files-add` 用可重复的 `--adapter-option ADAPTER.OPTION=VALUE` 传入选项（如 `--adapter-option epub.ruby_mode=aozora`）；Web 上传使用同名 `adapter_options` JSON。

两者构建同一形状，取值语义统一在宿主的导入与运行选项边界校验。CLI 的 `files-options` 可逐 File 查看/部分更新，或按 Adapter 对项目内全部当前 File 查看/部分更新。

### 契约测试

`tests/test_document_adapter_contract.py` 是外部 Document Adapter 的契约基准。它使用独立的
第三方风格 Adapter（`record`，`.rec`）覆盖：

- 按扩展名与显式 ID 导入、选项校验与透传；
- `opaque_state` 存储往返及 `part_id`/`model_source` 落地；
- 翻译时应用 `normalize_model_output`，以及双语和纯译文导出；
- 运行选项的 File 存储、动态渲染、冻结快照和指纹跟踪；
- Adapter 缺失、版本不匹配、状态损坏和能力不足。

任何标准第三方 Adapter 必须通过该套件的通用路径。

### 版本与升级策略

Adapter 默认只能读取与自身 `version` 相同的 File 状态。Adapter 可选声明 `readable_versions: frozenset[str]`，且必须包含当前版本；这只表示当前实现能安全解释旧状态，不会改写 File、`opaque_state`、Segment 或阶段结果。

File 版本与状态记录版本仍必须一致，未声明可读的版本立即失败。外部 Adapter 未声明时仍保持严格相等语义。

### 导出

宿主选择阶段结果、执行缺失结果规则和前导空白恢复，再逐 File 向来源 Adapter
提供该 File、Segment、目标文本、模式和不透明状态。Adapter 只能在给定 staging
目录生成相对路径；全部生成并验证成功后，宿主逐文件移动到正式输出目录。

Document Adapter 插件协议当前为版本 12。统一 TXT 导出由宿主改用内置 `txt`
Adapter 处理各 File，不调用来源 Adapter，也不解释来源格式状态。

Adapter 缺失、版本不一致、状态损坏、能力不足或运行异常都会终止当前操作。
不会自动改用 TXT，也不会删除仍可读取的项目 Segment 和阶段结果。

### SRT 0.1（外部插件示例）

SRT 插件位于 `plugins/srt/`，发行包名为 `another-llm-translator-srt`，通过
`another_llm_translator.plugins` entry point 注册。

每个 cue 是一个 Segment，所有 cue 使用 `document` part；`opaque_state` 只保存原始序号和时间行。

插件严格接受唯一正整数序号及 `HH:MM:SS,mmm --> HH:MM:SS,mmm` 时间行，序号不要求
连续，正文可以跨多行。单语导出替换 cue 正文，双语导出在同一 cue 中追加换行和译文。
输入换行会规范化为 LF，输出换行、末尾换行和编码由插件与宿主输出契约决定。

HTML/ASS 样式标记作为普通正文交给模型，插件不解析或保证保留；译文不得包含
空白分隔行，否则会改变 SRT cue 边界并进入现有格式失败流程。插件不接受缺序号、点号
毫秒或时间行尾定位参数等非核心变体。

### EPUB 0.6

EPUB Adapter 每次导入一个 `.epub`；同一项目可包含多个 EPUB File。Adapter 保存各 File 的原始容器，并记录 OPF、spine 顺序、导航资源以及 Segment 到 XHTML/NCX 文本流和 `text`/`tail` 槽位的定位。

每个 spine XHTML、EPUB 3 `properties="nav"` 导航 XHTML 和 EPUB 2/3 `spine toc` 指向的 NCX 的归档路径作为 Segment 的 `part_id`。非 spine 导航资源排在正文前；spine 内导航保持原位置且不会重复。普通透明内联元素中的相邻槽合并为一个复合 Segment；未知结构和 `br` 形成边界。导出只重写包含翻译 Segment 的 XHTML/NCX，保留导航链接、元数据、图片、CSS、字体和其他资源。

EPUB 3 导航 XHTML 的整个 `body` 可见文本会进入翻译；NCX 的 `docTitle`、`docAuthor` 和所有 `navLabel` 下的 `text` 会进入翻译。NCX 的 `content src` 等定位属性不会翻译。

导出时宿主提供 `target_language` 和可选的 `target_language_tag`。EPUB Adapter 要求语言标签为非空 BCP 47 标签，并要求目标语言名称非空。单语输出的 OPF `dc:language` 设为该标签；双语输出把该标签放在第一项，随后保留源语言。

已重写的 XHTML 同时更新根元素的 `lang` 和 `xml:lang`；NCX 更新根元素的 `xml:lang`。中文应使用 `zh-Hans` 或 `zh-Hant`，以便 Apple Books 识别正确的语言和字体。

译文和双语输出会生成基于项目、File、目标语言标签和输出模式的稳定独立出版标识，并将 OPF 主标题分别后缀为 `（目标语言）` 和 `（目标语言·双语）`。

同一输出再次导出时标识保持不变；每次导出会刷新 EPUB 3 的 `dcterms:modified`，或 EPUB 2 的 `dc:date opf:event="modification"`，用于阅读器缓存更新。源书原有标识和其他书籍元数据保留不变。

双语模式在单槽 Segment 中按“源文、换行、目标文本”写入；复合 Segment 保留
所有源槽，并在最后一个槽后追加“换行、目标文本”。body 声明
`white-space: pre-line`。该规则属于 EPUB Adapter，不是宿主通用排版树。

EPUB Adapter 只接受 OPF `package` 版本 `2.0` 或 `3.0`。

EPUB 3 XHTML 可无 DOCTYPE，或使用无外部标识的 `<!DOCTYPE html>`；EPUB 2 XHTML 可无 DOCTYPE，或使用 PUBLIC `-//W3C//DTD XHTML 1.1//EN`，SYSTEM 地址只作为声明数据而不会被加载。

NCX 可无 DOCTYPE，或使用 PUBLIC `-//NISO//DTD ncx 2005-1//EN` 与标准 `ncx-2005-1.dtd` SYSTEM 地址；其他 NCX 外部 DTD、实体声明和错误声明都会快速失败。

所有外部 DTD、实体声明、版本不匹配的声明、SYSTEM-only 和错误 PUBLIC 标识都会快速失败。

普通透明内联元素中的相邻文本槽构成一个复合 Segment；纯译文把整条译文写入首槽并清空其余槽，保留标签及 attrs 骨架，不猜测局部格式对应关系。双语导出保留源槽并在末槽后写入译文。

Ruby 是同一文本流中的内联成员；包含 Ruby 的复合 locator 可以按源文顺序混合普通 `text`/`tail` 槽和 Ruby 槽。新导入总是保存青空 Ruby 的规范 Segment 原文与完整 locator。`ruby_mode` 是 File 级运行选项，可选 `aozora`（默认）、`short_xml`、`compact` 或 `base_only`。

除 `base_only` 只从模型输入删除 Ruby/reading 外，用户 source 和阶段结果均使用青空 `｜base《reading》`；`short_xml` 只向模型使用 `<r><b>base</b><y>reading</y></r>`，`compact` 只向模型使用 `⟦R:base|Y:reading⟧`。这些转换与输出规范化都只发生在冻结的运行上下文。

新导入不提供 `parenthetical`。无法确定基础文字和读音的嵌套或残缺结构会带 XHTML 位置快速失败。

纯译文导出把整条译文写入混合 Segment 的首个可用位置，清空其余普通槽并删除该 Segment 内全部 Ruby；双语导出保留完整源句和 Ruby，并只在整个 Segment 末尾追加普通译文。

`ruby_mode=aozora` 时，模型可以省略译文、校对或润色结果中的 Ruby 标记和 reading，但必须翻译属于正文的 base，不能因其位于 Ruby 中而照抄。

保留时须返回严格闭合的 `｜已翻译base《目标语言适用reading》`，系统会在纯译文和双语译文区域恢复 EPUB Ruby；reading 必须翻译或转写，无法适配时应去掉标记和 reading，仅返回已翻译 base。没有返回 Ruby 不会触发重试；不完整、嵌套、含 HTML 或跨行的形式按普通文本保留。

`base_only` 不执行 Ruby 还原。short XML 使用标准 XML 实体；compact 在 base/reading 中用反斜杠转义 `\\`、`|`、`⟦`、`⟧`。两种模型格式的非法结构进入既有格式修复预算，不会泄露到用户文本。

Reading 完全由同一个 `·・ • ◦ ● ○ ◉ ◎ ▲ △ ﹅ ﹆` 组成时视为 Emphasis Ruby。直接相邻的同符号 Ruby 及单 Ruby 内的重复符号在用户/模型表示中合并为单个 reading；普通文本、空白和内联格式边界会切断合并。

最终 EPUB 导出按 Unicode 扩展字素簇展开，为每个非空白字素簇写入一个带相同 `rt` 的 Ruby；普通 reading 仍保持分组 Ruby。确定性术语注入使用同一严格青空语法：匹配视图分别保留 base 正文和 reading，不把二者拼接。

base 可跨相邻 Ruby 匹配连续正文，直接相邻 Ruby 的 reading 也会连续组合，普通正文会切断 reading 组合。因此 `｜漢《かん》｜字《じ》` 可命中 “漢字”和“かんじ”，而 `｜漢《かん》A｜字《じ》` 不会把 reading 拼成“かんじ”；同一术语从两个视图命中时只注入一次。

该匹配规则不改写 Segment 原文或发送给模型的 `source`。

`inline_format_mode=markers` 与 `inline_format_policy=tiered|strict` 也是 File 级运行选项。marker 在运行时生成，EPUB 不持久化依赖这些选项的 `model_source`；`plain` 是默认值，模型只看到净文本。

`tiered` 要求语义关键标签保留，表现层标签可整体省略；`strict` 要求全部源标签保留。

EPUB Adapter 仅在 `markers` 模式向对应请求的 Prompt 注入上述保留要求，并把受控标记校验交给自身：未知、重复、未闭合、错误嵌套或破坏父子关系的结果进入既有格式修复预算，耗尽后 Segment 失败。

其中 `tiered` 的语义标签为 `a`、`abbr`、`bdi`、`bdo`、`cite`、`code`、`data`、`dfn`、`kbd`、`q`、`samp`、`sub`、`sup`、`time`、`var`；表现层标签为 `b`、`em`、`i`、`mark`、`s`、`small`、`span`、`strong`、`u`，后者可整体省略。

纯译文仍使用原标签和 attrs 的空骨架写回，模型标记不会作为 HTML 直接写入 EPUB；详情界面同时显示净文本与模型文本预览。

每个 Run 的 `document_adapter_prompt_requirements.json` 和 manifest 会保存按 File
生成的本地化要求快照；`prompt.txt` 保存宿主基础 Prompt。请求实际使用的 Prompt
还会在分块、格式修复和翻译校验修复时按当前要求集合组装。

安全边界拒绝：

- ZIP 绝对路径、`..`、反斜杠路径、重复路径和符号链接；
- 过多条目、过大解压总量和异常压缩比；
- 越界或缺失的 OPF/spine 资源；
- 非法版本化 DOCTYPE、外部 DTD、实体声明和非法 XML。

## 3. 可信 Python 插件宿主（Beta）

插件包在 entry-point 组 `another_llm_translator.plugins` 注册一个
`PluginDescriptor` 实例或返回该实例的无参函数：

```toml
[project.entry-points."another_llm_translator.plugins"]
my_plugin = "my_package.plugin:descriptor"
```

```python
from app.plugins import PluginDescriptor

def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="my-documents",
        version="1.0.0",
        protocol_version=11,
        document_adapters=(MyDocumentAdapter(),),
    )
```

宿主拒绝重复/空插件 ID、重复/空 Adapter 或 Translation Validator ID、未知协议
版本和不完整声明。插件代码与宿主同进程运行，拥有当前进程权限；安装即表示
信任。插件不得自行操作 Run、限速器、项目 JSONL 或正式输出目录。

翻译校验器通过 `translation_validators` 注册。共享插件协议当前为版本 `11`；每个校验器声明唯一的 `validator_id`、`version`、`label`，并实现接收 `TranslationValidationContext` 的 `validate(context)`。

上下文只包含当前 Segment 的源文、候选译文和宿主确定的逐 Segment 术语命中，不包含项目路径、术语库对象或 Run。宿主会校验 finding 的译文边界，并把校验器及插件版本写入翻译阶段指纹。

`TranslationValidationMatch.severity` 为 `error` 或 `advisory`。

`error` 必须指向候选译文中的非空范围，使用现有修复与 `exhausted_mode`；`advisory` 可以表示缺失的建议而没有译文范围，宿主最多为每个 Segment 发起一次定向修复，仍未通过时保存为 warning。

首个真实外部示例是可选的 `another-llm-translator-term-validation`，提供 `preferred_term_usage`；它只检查实际命中的、带推荐译名的术语是否至少出现一次，不要求强制替换。

```python
from app.translation_validation import (
    TranslationValidationContext,
    TranslationValidationMatch,
)

class MyValidator:
    validator_id = "my_validator"
    version = "1.0.0"
    label = "My validator"

    def validate(self, context: TranslationValidationContext):
        return ()
```

导入选项只支持上述类型明确的单层 choice 声明；插件不得把它当作通用配置或
运行期设置。不要依赖未文档化的内部对象。

## 4. LLM Preset（已实现）

Preset 位于全局 `llm_presets/<preset_id>.json`，实时引用一个 Adapter ID，并保存 Base URL、模型、credential 引用、模型 Token 能力和端点限速等连接设置。项目配置一个全局 Preset，并可为术语、翻译、校对和润色分别选择覆盖；空覆盖使用全局 Preset。

Run 保存当前阶段实际解析的 Preset 快照，阶段指纹包含该 Preset ID 和影响阶段语义的定义内容 Hash；单独修改 `target_chunk_input_tokens` 不会改变该指纹。

当前 Preset schema 为 7。`stream` 明确控制是否使用所引用 Adapter 的 SSE 能力；普通与流式 Endpoint 均由 Adapter 定义。`target_chunk_input_tokens` 是完整输入 Prompt 的 Chunk 软目标；每个阶段使用其实际解析的 Preset 值，实际请求仍受上下文硬限制、Token 安全系数和启用的 ITPM 约束。RPM/ITPM 按每个 Key 独立计算，`max_parallel` 是 Preset 总并发上限，`max_parallel_per_key` 是所有 Key 共用的单 Key 并发上限。

schema 2–6 用户 Preset 在 CLI、Web 或桌面 sidecar 启动时原子迁移为 schema 7，补入缺失字段；旧 `endpoint` 与 `stream_endpoint` 字段原样保留，但不校验、不参与请求或指纹。`target_chunk_input_tokens` 的迁移默认值为 `8192`，`max_parallel_per_key` 默认为 `max_parallel`。Run 内历史 Preset 快照只在内存中补齐默认值，不改写审计文件；schema 1/2 Adapter 快照不兼容 schema 3，不从 Preset 回退 Endpoint。项目配置中曾出现的同名 Chunk 字段是遗留兼容字段：旧项目可继续读取和保存，但其值无效；新项目不再写入该字段。

启用流式但 Adapter 没有 `streaming` 规则时保存、创建 Run 和发送请求都会快速失败。

Preset 还可保存 `extra_body` JSON 对象，用于 OpenRouter provider order 等
端点专属请求字段：

```json
{
  "provider": {
    "order": ["anthropic", "google"],
    "allow_fallbacks": false
  }
}
```

宿主先渲染 Adapter 的完整 body，再添加 `extra_body` 的顶层字段。任一顶层
字段已存在于 Adapter body 时必须快速失败，不允许覆盖或递归合并。修改 Adapter
固有字段应复制或编辑 Adapter。

`extra_body` 不支持模板占位符，尤其禁止 `${api_key}`。它会进入请求预览、
Run 快照和阶段指纹，因此不得保存密钥。非对象、非法 JSON、占位符和字段冲突
都必须在创建 Run 或发送请求前拒绝。

Preset 也可提供可选的 `extra_headers` 对象，为 Provider 添加自定义请求头：

```json
{
  "x-opencode-session": "${session_id}",
  "x-opencode-request": "${request_id}"
}
```

`${session_id}` 使用当前 Run ID，`${request_id}` 使用当前 Req ID；附加 Header 只
用于 LLM 生成请求，模型列表探测不携带。固定值会保存，请勿填写 API Key；鉴权继续
使用 credential/Adapter。Header 名称与 Adapter 默认 Header 冲突（大小写不敏感）时
必须快速失败。

Preset 修改立即影响所有引用项目，不维护版本历史。Adapter 修改立即影响
所有引用 Preset。项目缺少其引用的全局 Adapter 时明确失败，不自动补齐或
改用其他 Preset。项目、全局模板和 Run 续作只接受命名 Preset，不支持内联
连接配置。

### Preset 多 API Key

Preset 仍只记录一个 credential 引用；其环境变量或钥匙串值按行解析为 API Key
列表。每个 Key 独立使用 RPM/ITPM 窗口和单 Key 并发上限，同时受 Preset 总并发
上限约束。401/403 只隔离本次执行中的当前 Key，429 冷却并轮换，400/404 或协议、
配置错误直接失败，不提供 Provider fallback。Run 收尾会按 Key 追加安全审计，绝不
保存 Key 原文、摘要或跨执行健康状态。
