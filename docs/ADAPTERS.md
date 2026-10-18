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
  "schema_version": 4,
  "response_format": "json-pointer",
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

该字段用于 JSON Pointer 模式；原生 Chat Completions 模式按上述顺序读取两种字段。JSON Pointer 定义可
使用单数的 `response_reasoning_content_pointer`，但单数与复数不能同时配置。候选
路径只在前一路径缺失时继续尝试；首个存在的 `null` 表示没有 reasoning，多个字段
不会拼接。

需要把规范化的 system/user/assistant 消息转换为 Provider 原生形状时，可设置
`messages_format`（可选，默认 `openai` 原样透传）：

- `responses`：保留 input 消息和文本，将 system 角色改为 developer；
- `anthropic`：system 消息剥离到顶层字段，其余 user/assistant 消息保留
  字符串 content；
- `gemini`：system 消息剥离到顶层字段，user/assistant 映射为 `contents`
  数组的 `role: "user"/"model"` 与 `parts: [{"text": ...}]`。

`body` 模板可使用 `${system}` 占位符读取剥离后的 system 文本（多条消息以空行拼接）。
例如，Anthropic 使用顶层 `system`，Gemini 使用 `system_instruction`。

该占位符对 `openai` 格式同样可渲染，但 system 消息仍保留在 `${messages}` 中，
通常不应同时使用。

### Adapter 能力与固定连接

宿主从请求模板中的 `${temperature}`、`${max_output_tokens}` 判断参数支持；
声明 SSE 且顶层 `body.stream` 固定为 `true` 时要求流式，否则由 Preset 选择。
Adapter 摘要通过 `capabilities` 返回这些能力，Preset 编辑器据此展示字段。
可选 `connection` 对象包含固定 `base_url`、`credential: {kind, name}` 和
`proxy_source: "preset" | "connection"`；保存与执行必须满足这些约束。
当前授权连接代理由 `chatgpt` 凭据提供。
连接描述不替代凭据模块对令牌目标和授权的安全校验。

### JSON Schema 结构化输出

Adapter 可选 `structured_output` 是 JSON Pointer 到请求值模板的非空映射。模板仅支持独占字符串值的
`${json_schema}`，用于插入任务声明的完整 Schema。例如 Chat Completions：

```json
{
  "structured_output": {
    "/response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "structured_response",
        "strict": true,
        "schema": "${json_schema}"
      }
    }
  }
}
```

宿主复用请求字段写入逻辑按 Pointer 插入，保留同一父对象内的其他字段；与 Adapter body、流式 body
或 Extra Body 的目标字段冲突时失败。`capabilities.json_schema` 表示 Adapter 是否声明此能力。
内置 OpenAI Chat Completions 使用 `response_format`，Responses 使用 `text.format`，见
[OpenAI 结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs)；Anthropic 使用
`output_config.format`，见 [Claude 结构化输出](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)；
Gemini Generate Content 使用 `generationConfig.responseMimeType` 与 `responseJsonSchema`，见
[GenerationConfig](https://ai.google.dev/api/generate-content#v1beta.GenerationConfig)。具体模型或端点不支持时明确失败，不改用其他模式。

LLM Preset 的可选布尔值 `json_schema_enabled` 默认 false，缺字段的已有 Preset 视为关闭，
可在编辑器手动开启并保存，资源版本不变。开启后要求 Adapter 声明能力。
`LLMClient.chat` 和 Adapter `build_request` 接收可选 `json_schema`；客户端仅在 Preset 开启且任务
提供 Schema 时发送约束。任务未声明 Schema 时沿用自身输出契约，JSONL 阶段不作格式转换。
调用方按同一 Adapter 渲染包含 Schema 的完整请求进行 Token 估算；请求正文与 Schema 一同进入诊断及已启用的 Debug 保存。
Preset 开关和 Adapter 声明随现有资源快照保存并参与指纹。Schema 约束不替代任务自己的结果校验。

### ChatGPT Plan 连接

`credential: {"kind": "chatgpt", "name": "default"}` 必须配对 `chatgpt-plan` Adapter。
该组合固定 `base_url: "https://api.openai.com/v1"`、`/responses` 端点、
`messages_format: "responses"`、`stream: true`、`store: false`，Preset 的 `proxy_url` 留空；
实际代理由唯一 ChatGPT 连接提供。请求每次尝试前获取有效 access token，令牌不进入
Preset、Run 快照或调度身份。令牌刷新不改变连接身份；重新登录产生新会话，旧任务不能继续请求。

模型目录从账户返回的 `models` 中读取 `slug` 与 `display_name`，只列出 `visibility: "list"`。
模型发现允许尚未选择模型的草稿；保存和执行要求明确模型。`max_output_tokens` 仅供本地
分块估算，temperature 不参与 Plan 请求或阶段指纹。额外请求体中的不支持字段直接拒绝。
额度耗尽、授权失效及账户或能力不符合条件停止执行；临时网络及服务错误使用现有有界重试。

授权遵循官方 [Sign in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)、
[Sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions) 与
[Inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) 规范。
动态注册按用户数据根目录保存稳定 host ID 和各账户 client ID；重授权复用注册并验证原账户。
ID Token 验证签名、issuer、audience、expiry 和 nonce，授权以返回 scopes 为准。
刷新通过现有跨进程写锁串行化并重新读取钥匙串令牌。退出撤销 renewable session 并删除本地令牌，
远端撤销未确认时显示结果。凭据保存失败直接报错。

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

schema 4 的 JSON Pointer Adapter 可以增加 `streaming` 对象；宿主在全局 Adapter 摘要中以
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

内置 `openai-compatible` 同时接受 `[DONE]` 和显式启用的 clean EOF；Responses、Gemini、Anthropic 使用各自声明的终止事件。声明的截断或拒绝结束原因也允许在自然 EOF 时完成传输判定。宿主不识别 `cost` 或其他供应商字段作为隐式终止标记。

### 响应边界

JSON Pointer 模式的 `response_content_pointer` 是必需的 RFC 6901 JSON Pointer，结果必须是字符串或 null；null 规范化为空正文。只有明确命中声明的截断或拒绝结束原因时，正文路径缺失才规范化为空正文。JSON Pointer 的数组索引 token 支持负索引 `-N`（RFC 6901 扩展）：`-1` 为最后一个元素、`-2` 为倒数第二。

原生 `response_format` 可选 `chat-completions`、`responses`、`anthropic`、`gemini`；宿主聚合各协议的全部正文文本，Gemini 仅取首个 candidate 并排除 thought。原生模式不能同时声明正文、思考正文、结束原因或 usage 映射。原生 streaming 只声明 `transport`、`endpoint`、可选 `request_body` 和 `allow_clean_eof`，事件解析由响应格式决定。

Chat Completions 使用 `[DONE]`，Responses 使用 completed/incomplete，Anthropic 使用 message_stop，Gemini 收到 finishReason 后继续读到自然 EOF，以收集后续 usage。拒绝响应优先失败，即使已经收到部分正文，也不触发空正文重试。截断响应进入已有的有界截断重试策略。

也可使用非空的 `response_reasoning_content_pointers` 数组声明有序候选路径：路径缺失时继续尝试，首个存在的 `null` 规范化为 null，字段存在但类型错误时当前请求失败，不猜测或拼接多个字段。

可选的 `response_finish_reason_pointer` 提取字符串或 null 的结束原因；路径缺失时无结束原因。
`truncated_finish_reasons` 与 `blocked_finish_reasons` 分别声明长度限制与拒绝生成的结束原因字符串列表，
缺省为空，两个列表不得重叠。宿主只按这些明确声明分类空正文，不推断供应商或思考预算。

流式 `streaming.finish_reason_events` 使用与 `content_events` 相同的 `pointer`、可选 `when` 结构，
缺省为空；保留最后一个匹配的结束原因。自然 EOF 前明确收到声明的截断或拒绝结束原因时，
可结束传输并交由宿主处理空正文；读取或协议错误仍按原规则处理。

Adapter 规范化返回 `content`、可空的 `reasoning_content` 和可空的 `finish_reason`。宿主随后按统一严格规则，
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
- 原生 Anthropic 与 Gemini 格式读取全部分页，重复游标或任一页失败时整体报错；Gemini 仅保留支持 `generateContent` 的模型。
- `response_models_pointer` 指向模型条目数组；`response_model_id` 是条目内
  必填键名；`response_model_display` 与 `response_model_strip_prefix` 可选。
- 展示名缺失时回退为模型 ID；`response_model_strip_prefix` 从模型 ID 前缀
  剥离（如 Gemini 的 `models/`）。条目缺少 ID 或响应形状非法时快速失败。
- 缺少 `models` 规格、缺失或格式错误的 credential、HTTP 错误或网络异常都明确
  报告，不猜测、不 fallback。请求使用 Preset 的代理与超时设置。

JSON Pointer Adapter 可声明可选的 `usage` 映射，把端点响应中的消耗换算为规范化计数：

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
  `manifest.json` 的 `usage` 字段。JSON Pointer Adapter 未声明 `usage` 时任务与 Run
  摘要不包含 usage 字段。
- 主请求的限速、重试与错误语义不受影响。

原生模式直接读取供应商用量字段。Anthropic 输入包含基础输入、缓存读取和创建，输出使用已包含 thinking 的 `output_tokens`；Gemini 输出包含 candidate 与 thoughts。流式累计计数采用最终值，非法计数直接报错。

### 内置 Adapter 定义

| Adapter | 请求协议 | 原生响应格式 | 输出上限字段 |
| --- | --- | --- | --- |
| `openai-compatible` | Chat Completions | `chat-completions` | `max_tokens` |
| `openai-chat` | OpenAI Chat Completions | `chat-completions` | `max_completion_tokens` |
| `openai-responses` | Responses | `responses` | `max_output_tokens` |
| `chatgpt-plan` | Responses，固定授权连接 | `responses` | 仅用于本地预算 |
| `anthropic` | Messages | `anthropic` | `max_tokens`，最小值 1 |
| `google-gemini` | generateContent | `gemini` | `generationConfig.maxOutputTokens` |

Anthropic 输入 token 包含未缓存、缓存创建和缓存读取，total 由实际输入和输出相加；流式 usage 使用最终累计值。Gemini 保留官方 totalTokenCount，不以可见正文 token 重算。原生拒绝、promptFeedback 拦截和流内失败保持显式，供应商错误消息不会变成通用缺少正文路径。

版本前缀 `/v1`、`/v1beta` 写在 Preset `base_url` 中。普通和流式 endpoint 仍由 Adapter 声明。

### JSONL 分隔符容错

模型仍应返回以换行分隔的 JSONL。宿主也接受完整 JSON 对象之间以半角逗号分隔，允许与换行混用；记录内容和最终 `end` 校验保持不变。采用此容错时，Run 警告记录“已接受逗号分隔的 JSONL 响应”。

### 术语提取实验响应

术语＋粗翻、术语＋概括和术语＋粗翻＋概括均使用显式术语声明：有术语返回 `term` 记录；无合格术语必须返回唯一的 `{"type":"no_terms"}`，不能附加字段或与 `term` 并存。末行仍为 `{"type":"end"}`，只表示结束。实验中的单独补扫请求也遵守这一规则；仅补译或仅补概括请求不声明术语结果。缺少或无效的声明属于整次响应的格式错误。

组合实验与标准术语扫描共用字段校验：`source`、`category` 为非空字符串，`description`、`preferred_translation` 为可选字符串，`aliases` 为字符串数组。宿主不额外检查候选及别名是否在源文中连续出现，也不额外拒绝重复候选或术语记录的未知字段。

合并请求使用规范的 Prefix 和 Suffix 包围当前语言的对应 Prompt。需要返回片段引用时，输入在对应文本对象中携带请求内短 `id`，不另发 ID 数组。仅术语请求不携带片段 ID。粗翻为每个请求 Segment 返回一条完整 `segment` 译文记录；概括返回覆盖 `source_segments` 中 `id` 的 `summary` 记录，且出现在 `term` 或 `no_terms` 之前。三项合并时同时返回这些结果，末行只输出一条 `end`。仅补译与概括时返回 `segment` 和 `summary`，不声明术语结果。响应均遵循严格 JSONL。

### 自动术语决策关系校验

名称与关系决策各阶段的 `description` 为按术语当前归属拼接的全部有效、未停用说明片段，每段以方括号标明原术语归属；`applied_description` 单独提供已应用实体说明作对照。两者均只读。片段可能重复、存在分歧或描述不同位置，排列顺序不表示事实发生时间。拼接文本计入现有分批预算，不截断。

`changes` 按完整最终状态检查 [术语组规则](MINIMAL.md#51-术语)，覆盖新增 Alias、
组关系变更和成员重新启用。新增非法关系返回 `invalid_relationship`；普通决策无法完整
解决时使用 `needs_review`，自动终审仍必须给出合法最终状态，否则执行失败。
受保护条目的名称等字段只作为只读证据，实体说明允许提出更新。草案应用时对实际接受的组合重新检查，未解决的受影响关系拒绝写入。
在此前阶段的最终状态上，额外以 `containment` 请求复核原文长短包含而译名疑似不一致的组合。
子串关系只筛选候选，不构成硬冲突；可明确保留独立译法，无法可靠判断则将关联组件恢复到原状态并进入 `needs_review`。
同一复核组合中发生的修改作为整体建议接受或拒绝；复核不能修改受保护条目，不能通过修改制造新的未确认包含冲突。
此复核位于可选自动终审之后，仍可产生待审项。当前决策规则版本为 12；旧 Run 和旧规则草案不作转换，须新建 Run。

完整合法 JSONL 中的未知 `normalized` 会明确拒绝并记录警告，不影响本次其他目标。缺少、重复或内容校验失败的目标按原状态与候选状态间的完整关系依赖组件重试；依赖失败的已接受结果一并撤回。损坏 JSONL 或缺少 `end` 仍使整份响应无效。重试请求只附带相关 anchors，按完整请求重新计算预算。`update` 参考应用后的状态，`disable` 参考已禁用状态，`needs_review` 参考本阶段待审状态，`keep` 参考当前状态。

重复相同错误触发的隔离修正会在同一轮使用固定的已接受状态快照，并受现有 `max_parallel` 限制；各请求结果统一合并后检查组合关系。若组合引入关系冲突，会撤回受影响依赖组件并在既有格式修正轮数内重试。

固定 Prefix 按阶段声明输入字段、只读参考与任务，固定 Suffix 按阶段声明输出协议；JSONL 响应追加一次结束约束。可编辑中段位于两者之间，负责信息取舍、详略与表达策略；来源边界、引用及输出覆盖要求由固定协议定义。联合模式声明参与阶段和附加输出协议；设置预览与实际请求共用组装入口，固定规则版本纳入 fingerprint。名称与关系阶段的 `description` 和 `applied_description` 均为只读。

### 实体说明决策与接口

完整自动决策在名称与关系确定后，或仅实体说明模式直接运行时，`phase: "knowledge"` 发送 `target_language`、`description_generation` 和 `entities` 数组；各实体包含 `entity`、`members`、`current_items` 和全部有效 `fragments`。片段包含整次请求内唯一的短 ID（如 `K1`）、原归属、当前归属、说明及来源范围。实体证据完整且不可拆分，按现有软目标、硬限制和安全系数装箱，批次共用现有并发调度与 KeyPool。超限实体单独失败。Prompt 使用独立资源 `terminology_knowledge` 的中段，不携带名称与关系决策中段；该资源不是独立执行阶段或模型设置。

Web 的任务启动与 task-options 接口在 `stage: "terminology_decision"` 下接受布尔 `knowledge_only`（默认 `false`）。`true` 只执行实体说明阶段，不能与 `final_review: true` 组合，Run、检查点及草案记录该标记。

请求中的 `source_range` 将连续源文表示为区间，每项包含 `file_id`、`part_id`、`first: {position, start}` 和 `last: {position, end}`。`position` 是从 0 开始的 Segment 位置，偏移是首尾 Segment 内的字符位置，`end` 为不包含端点。跨文件、分部、位置间断或切片空缺分别保留。无来源片段为 `null`。持久化来源和引用校验仍使用完整 Segment 范围。

每个请求实体恰好返回一条实体记录，允许乱序，最后返回终止记录：

```jsonl
{"type":"entity_knowledge","entity":"alice","items":[{"text":"船长","refs":["K1"]}],"reason":"源文明确身份"}
{"type":"end"}
```

`description_generation` 为 `sourced` 时使用上述格式；`sourced_and_summary` 额外要求一个非空字符串 `summary`，概括全部有效证据中的核心信息；`summary_only` 要求 `items` 为空且 `summary` 为一份非空精简总结。总结保存为 `fragment_ids: []` 的无来源条目，不加入原始证据集，宿主单独记录其证据依赖以保持过期检查。

字段严格校验，`items` 中条目文字非空且引用非空。完整文档中的缺失或重复实体、非法条目、未知或跨实体引用仅使对应实体失败；成功实体立即保存检查点，只重试未完成实体，格式反馈纳入预算并重新分批。未知实体、损坏 JSONL 或缺少终止记录使整次响应无效。停用或失效证据不发送。宿主将短引用转换为持久化 `fragment_ids`；扫描片段以候选记录 ID 和条目序号标识，并关联原 Segment、实际输入切片范围及原文/模型输入摘要。

术语查询附加 `entity_key`、`knowledge_status`（`current`、`pending`、`stale`）、`description_items`，保留只读 `description` 投影。条目只返回 `text` 和 `fragment_ids`。`GET /api/v1/projects/{name}/terms/knowledge?normalized=...` 返回条目、片段和导航所需的引用范围，不暴露来源摘要。

翻译、校对、润色和联合粗翻的 `entity_knowledge.items` 提供 `text`、`source_relation` 和布尔 `unsourced`，每个命中实体只提供一次按注入策略筛选后的已应用条目，筛选语义见 [产品规格](MINIMAL.md#实体说明实验)。`source_relation` 最多包含三个指示，例如 `["before 1", "within", "after 2"]`；无来源说明使用空数组及 `unsourced: true`，混合引用同时保留相对关系和无来源标记。位置以本次待处理源文的首尾范围为准，使用原 Segment 位置和实际切片偏移，结束偏移不包含端点；范围内空缺归为 `within`，参考上下文不扩大范围。`before`、`after` 分别按同一实体各条目的最早相关证据位置升序编号，同位置同号，`within` 不编号。位置和编号表示证据先后，不表示事实生效范围或事件时间。生成请求不发送具体来源区间或当前位置数组；持久来源、实体详情和导航保留完整范围。

`PUT` 同一路径使用现有写锁和事务，必须提供 `normalized`、`expected_revision`、`action`，操作字段分别为：

- `save_items`：`items: [{text, fragment_ids}]`，空引用明确创建无来源人工片段。
- `set_fragment_enabled`：`fragment_id` 和布尔 `enabled`。
- `confirm_keep`：仅接受有效的当前已应用说明。

术语编辑可携带 `description_items`，同时必须提供 `expected_revision`（新库为 0），旧 `description` 写入被拒绝。强制术语扫描有相交片段时必须提供 `confirm_knowledge_replacement: true`，CLI 对应 `--confirm-knowledge-replacement`。

## 2. Document Adapter（Beta）

Document Adapter 是同一格式的导入与导出边界。当前内置 `txt` 与 `epub`；官方目录插件
`plugins/srt/` 提供 `srt` Adapter：

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

    def segment_format_count(
        *, segment: dict, opaque_state: dict | None
    ) -> int: ...

    def replacement_options(
        *, opaque_state: dict | None
    ) -> dict[str, str]: ...

    def import_sources(...) -> DocumentImport: ...
    def export_sources(...) -> list[Path]: ...
```

可选属性 `readable_versions: frozenset[str]` 声明当前实现可安全读取的历史版本；未声明时只读取与 `version` 相同的状态，声明时必须包含当前 `version`。

可选的模型输出规范化方法签名为：

```python
def normalize_model_output(
    *,
    segment: dict,
    text: str,
    stage: str,
    opaque_state: dict | None,
    run_options: dict[str, str],
) -> str: ...
```

未实现时宿主原样使用模型文本。

`export_sources` 还会收到宿主项目配置中的 `target_language: str` 和 `target_language_tag: str`。前者是供模型和人阅读的自由文本名称，后者是可选的 BCP 47 输出语言标签；两者职责分离。Adapter 可以忽略、应用到自己的格式元数据，或在标签为空时明确拒绝导出。

宿主不按 Adapter ID 推断语言行为。Document Adapter 插件协议版本为 `13`；旧协议插件会快速失败，不保留旧调用路径。

`render_model_source` 在每个阶段开始时以同一个 File 的 `opaque_state` 和冻结的 `run_options` 生成模型源文。`model_prompt_requirements` 接收完全相同的快照；宿主可据此把不同要求集合拆分到不同 Chunk。`normalize_model_output` 是可选方法；实现时接收相同快照，未实现时宿主原样使用模型文本。要求不得包含源文、项目路径、凭据或动态用户内容。无专属格式要求时返回 `None`。

内置 TXT 与 SRT 插件没有额外的格式 Prompt 要求，返回 `None`，并原样返回模型源文。

所有基于纯文本的 Document Adapter 都可以复用宿主提供的严格字节解码 API：

```python
from app.plugin_api import DecodedPlaintext, decode_plaintext

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

Adapter 默认只能读取与自身 `version` 相同的 File 状态；可选的 `readable_versions` 语义见上文。这只表示当前实现能安全解释旧状态，不会改写 File、`opaque_state`、Segment 或阶段结果。

File 版本与状态记录版本仍必须一致，未声明可读的版本立即失败。外部 Adapter 未声明时仍保持严格相等语义。

### 导出

宿主选择阶段结果、执行缺失结果规则和前导空白恢复，再逐 File 向来源 Adapter
提供该 File、Segment、目标文本、模式和不透明状态。Adapter 只能在给定 staging
目录生成相对路径；全部生成并验证成功后，宿主逐文件移动到正式输出目录。

Document Adapter 插件协议当前为版本 13。统一 TXT 导出由宿主改用内置 `txt`
Adapter 处理各 File，不调用来源 Adapter，也不解释来源格式状态。

Adapter 缺失、版本不一致、状态损坏、能力不足或运行异常都会终止当前操作。
不会自动改用 TXT，也不会删除仍可读取的项目 Segment 和阶段结果。

### SRT 0.1（外部插件示例）

SRT 插件位于 `plugins/srt/`，作为官方目录插件随宿主资源分发。

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

插件使用的宿主契约统一从 `app.plugin_api` 导入；该模块只公开稳定的协议类型、严格解码
API 和插件所需错误类型，不提供插件发现或业务执行入口。官方插件和用户插件均是一个
直接位于插件根目录下的可信 Python 包：

```toml
schema = 1

[plugin]
id = "my-documents"
version = "1.0.0"
protocol = 17
entrypoint = "plugin:descriptor"
```

目录同时包含 `__init__.py`、入口模块和入口模块需要的相对导入文件。宿主只扫描官方
资源 `plugins/` 和用户数据目录 `<用户数据目录>/plugins/` 的直接子目录；子目录按名称
稳定排序，用户目录不存在表示没有用户插件。源码运行和冻结运行使用各自随应用分发的
官方 `plugins/` 资源。用户插件修改后重启应用生效。

宿主先读取并校验全部 `plugin.toml`，再执行任何插件代码。未知字段、缺失文件、协议不符、
重复插件 ID、入口不在插件目录内、描述符元数据不一致或入口失败都会带路径、阶段和原因
直接阻止启动；不跳过插件，也不回落到另一种发现机制。插件代码与宿主同进程运行，拥有
当前进程权限；目录命名空间只隔离模块名，不构成安全沙箱。

```python
from app.plugin_api import PluginDescriptor

def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="my-documents",
        version="1.0.0",
        protocol_version=17,
        document_adapters=(MyDocumentAdapter(),),
    )
```

宿主拒绝重复/空插件 ID、重复/空 Adapter 或 Translation Validator ID、未知协议
版本和不完整声明。插件代码与宿主同进程运行，拥有当前进程权限；安装即表示
信任。插件不得自行操作 Run、限速器、项目 JSONL 或正式输出目录。

翻译校验器通过 `translation_validators` 注册。共享插件协议当前为版本 `17`；每个校验器声明唯一的 `validator_id`、`version`、`label`、`phase`、`scope`，并按执行粒度实现同步或异步校验。

上下文包含当前 Segment 的 ID、源文、候选译文、逐 Segment 术语命中，以及可选的宿主 `DecisionService` 和独立的修复置信度门槛、警告阈值（`decision_warning_threshold`）。术语命中包含分类和说明；上下文不包含项目路径、术语库对象或 Run。宿主会校验 finding 的译文边界，并把校验器及插件版本写入翻译阶段指纹。

`phase` 为 `mechanical`、`alignment` 或 `terminology`，宿主按此顺序执行；同层按 ID 排序，完成整层后存在任何 finding 就阻挡后续层。`scope=segment` 使用 `validate(context)`；`scope=response` 使用 `validate_response(contexts)`，输入为按请求顺序排列的完整 Segment 上下文元组，返回以输入 Segment ID 为键、finding 元组为值的字典。手动编辑只执行逐 Segment 校验。

`TranslationValidationMatch.severity` 为 `error` 或 `advisory`。

`error` 必须指向候选译文中的非空范围；候选译文为空时使用空文本和 `start=end=0`。硬错误使用现有修复与 `exhausted_mode`；`advisory` 可以表示缺失的建议而没有译文范围，宿主最多为每个 Segment 发起一次定向修复，仍未通过时保存为 warning。advisory 的 `repairable` 默认 `true`，设为 `false` 时只保存 warning，不进入修复请求；error 必须可修复。

每个校验器必须实现同步方法 `retry_instruction(match_type: str, language: str) -> str`。
`match_type` 为该校验器产生的错误类型，`language` 为本次 Prompt 语言（`zh-CN` 或 `en`）。
返回值是只依赖当前请求的简短指令，不引用旧响应，不重复源文、术语数据或完整协议；空字符串明确表示不附加指令。
宿主只对本次重试目标的可修复 finding 调用该方法，按校验器和错误类型去重调用，再按指令文本去重写入 `validation_repair`。
缺少方法、返回非字符串或方法执行失败时明确失败，不回落到错误标记。旧协议插件须更新 manifest 和描述符版本，不保留兼容路径。

首个真实外部示例是可选的 `plugins/term_validation/` 目录插件，提供
`preferred_term_usage`；它先检查实际命中的推荐译名是否缺失，可显式启用 Decision 语义复核。

```python
from app.plugin_api import (
    TranslationValidationContext,
    TranslationValidationMatch,
)

class MyValidator:
    validator_id = "my_validator"
    version = "1.0.0"
    label = "My validator"
    phase = "mechanical"
    scope = "segment"

    def validate(self, context: TranslationValidationContext):
        return ()

    def retry_instruction(self, match_type: str, language: str) -> str:
        return ""
```

导入选项只支持上述类型明确的单层 choice 声明；插件不得把它当作通用配置或
运行期设置。不要依赖未文档化的内部对象。

## 4. LLM Preset（已实现）

Preset 位于全局 `llm_presets/<preset_id>.json`，实时引用一个 Adapter ID，并保存 Base URL、模型、credential 引用、模型 Token 能力和端点限速等连接设置。项目配置一个全局 Preset，并可为术语、翻译、校对和润色分别选择覆盖；空覆盖使用全局 Preset。

Run 保存当前阶段实际解析的 Preset 快照，阶段指纹包含该 Preset ID 和影响阶段语义的定义内容 Hash；单独修改 `target_chunk_input_tokens` 不会改变该指纹。

当前 Preset schema 为 8。`stream` 明确控制是否使用所引用 Adapter 的 SSE 能力；普通与流式 Endpoint 均由 Adapter 定义。`target_chunk_input_tokens` 是完整输入 Prompt 的 Chunk 软目标；每个阶段使用其实际解析的 Preset 值，实际请求仍受上下文硬限制、Token 安全系数和启用的 ITPM 约束。RPM/ITPM 按每个 Key 独立计算，`max_parallel` 是 Preset 总并发上限，`max_parallel_per_key` 是所有 Key 共用的单 Key 并发上限。

旧协议不在运行期自动转换。全局 schema 7 Preset 使用 `python -m app.llm_migration` 预览，添加 `--apply` 后备份并迁移；历史 Run 快照不改写，旧协议 Run 只供查看，正式执行需创建新 Run。

`send_temperature` 为必填布尔值；关闭后省略请求模板中的温度，禁止在 Extra Body 中再次添加。阶段温度不参与关闭发送后的阶段指纹。`reasoning_effort` 为字符串或 null，null 不发送参数；等级必须属于 Adapter 的声明，不能在 Extra Body 中重复填写声明位置或别名位置。Effort 不自动调整温度、thinking 开关或思考摘要。

内建 Effort 写入位置：Chat Completions 为 `/reasoning_effort`，Responses 与 Plan 为 `/reasoning/effort`，Anthropic 为 `/output_config/effort`，Gemini 为 `/generationConfig/thinkingConfig/thinkingLevel`。等级列表来自 Adapter 声明；具体模型不支持时保留供应商错误，不根据模型名自动转换。Gemini 2.5 示例保持 null，不将等级换算为 thinkingBudget。

Adapter 可声明 `max_output_tokens_minimum` 非负整数，默认 0；宿主按该值校验输出上限。可选 `reasoning_effort` 对象包含 `body_pointer`、非空唯一字符串数组 `values` 和可选 `aliases` JSON Pointer 数组。请求在完成 Extra Body 合并后向声明路径写入 Effort，仅创建必要的 JSON 对象，已有叶子字段或非对象中间路径直接失败。

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


## 5. Decision Preset 与服务（实验）

HTTP 模式的全局 `decision_presets/<preset_id>.json` 保存 `preset_id`、`protocol`、`url`、`model`、
`credential`、`proxy_url`、`context_window_tokens`、`context_safety_margin_tokens`、`token_safety_factor`、
`request_timeout_seconds`、`requests_per_minute`、`max_parallel`。
`protocol` 为 `typesafe` 或 `openai-decisions`；`url` 是含最终 Path 的完整 HTTP(S) URL，
宿主原样 POST，不推断或拼接路径。URL 不允许凭据、查询参数或 fragment。
凭据使用现有 environment/keychain 引用，密钥由宿主在请求时读取。
`proxy_url` 支持无凭据的 HTTP/HTTPS 代理；留空使用默认代理设置。
`context_window_tokens` 为正整数，`context_safety_margin_tokens` 为非负整数且小于窗口。
`token_safety_factor` 为有限正数，Example Preset 默认 1.25。
宿主用现有 Token 估算算法计算完整请求（证据、问题及选项）的输入量，乘以安全系数后向上取整；
超过窗口减去安全余量时，在发送前明确失败，错误包含估算量、可用容量及传入的 Segment ID。
仓库附带 TypeSafe 和 OpenAI Decisions Example Preset，分别使用
[TypeSafe 官方接口](https://api.typesafe.ai/redoc)和
[OpenAI 官方接口](https://developers.openai.com/api/docs/guides/decisions)。

LLM 模式的 `protocol` 为 `llm`，Preset 仅保存 `preset_id`、`protocol`、`llm_preset`、
`prompt_language`（`zh-CN` 或 `en`）。连接、凭据、Adapter、流式响应、上下文窗口、Token
安全系数、限流及输出上限由所选 LLM Preset 管理，请求温度为 0（仅在 Preset 允许时发送）。
用户消息包含 `state` 和 `questions`，正文仅返回含 `answers` 的 JSON 对象。宿主使用所选
LLM Preset 的模型标识填入 `model`，再复用 TypeSafe 响应校验，包括完整问题及选项覆盖、有限概率、概率和及显式拒答。
Decision 在 Preset 开启 JSON Schema 时按本次问题生成约束，固定问题名称、选项及概率字段，并包含拒答分支；
完整请求预算包含 Schema，概率取值、概率和及置信度仍由现有校验器检查。
请求与格式错误共享 `retry.http_max_attempts` 发送预算；空响应沿用 LLM 的专用重试规则。

HTTP Decision 用量读取响应 `usage.input_tokens`，仅接受非负整数，输出 Tokens 计为 0；LLM Decision 使用所选 Adapter 返回的输入、输出计量。各次已返回用量的尝试均计入用量，即使答案格式校验失败。两种模式均计入 Decision 请求用量，与生成 LLM 请求用量分开。

`decision_llm` 使用普通 LLM 提示词资源机制：Prefix 按字段声明拼接输入结构及指令边界，
中段定义判断策略和拒答条件，Suffix 定义输出结构及字段约束。中段按语言提供全局资源、
项目覆盖、恢复继承、内置草稿及命名模板。运行冻结完整系统提示词、所选 LLM Preset 和 Adapter，
保存在 Decision Preset 快照的 `_resources` 中，参与指纹及续作资源解析；不写入密钥。
缺失资源或无效输出明确失败，不降级到其他协议。

TypeSafe 将共享证据写入 `state`，问题写入 `questions` 映射，读取 `answers` 映射。
OpenAI Decisions 将共享证据编码为 JSON 字符串写入 `input`，问题写入带唯一 `name` 的
`questions` 数组，按名称读取 `answers` 数组。首版只支持 Choice，统一为
`DecisionQuestion(name, instructions, choices)` 和
`DecisionAnswer(choice, probabilities, confidence, refused)`。
插件通过 `await context.decision.choose(state, questions, segment_id=context.segment_id)` 调用，
可传入按时间排列的 `reference_context` 原文列表；此时 `state` 必须为对象，
宿主将上文写入证据的 `reference_context`。完整请求超限时从最远上文逐个移除并重新估算，
直至可发送或上文为空。缩减情况写入日志及 Run 判断记录，请求详情显示实际发送内容。
不得自行实例化客户端、管理凭据或发送 HTTP 请求。宿主负责限速、重试、取消、诊断和用量。
空响应、JSON 解析失败、回答缺失或重复、选项错误及非法概率，与网络异常、HTTP 429 和 5xx 共用 `retry.http_max_attempts` 及指数退避；预算耗尽后明确失败。每次尝试记录到诊断及已启用的 Debug 保存中。明确拒答返回 `refused=True`。

`validation.translation` 的 `decision_enabled` 默认 false，`decision_preset` 默认空字符串，
`decision_confidence_threshold` 与 `decision_warning_threshold` 默认均为 0.8，后者为 0 时关闭不确定警告；`decision_context_enabled` 默认 false，
`decision_previous_segments` 默认 1，为非负整数。启用上文时，
`TranslationValidationContext.previous_source` 提供同一 File、同一分区内前序非空 Segment 的原文。
推荐译名复核的 `terms` 列出本次提问的术语，`matched_terms` 只保留未提问的其他命中术语，供重叠关系判断。
推荐译名复核需要安装并选择 `preferred_term_usage`。`decision.preset` 提供通用连接；校验器的 `decision_preset` 为空时继承通用连接，非空时独立覆盖。置信度及上下文设置按校验器独立配置。Run 在 `decision_presets/<preset_id>.json` 保存所有实际使用的连接；同一 Preset 共用客户端与限流。实际连接、校验器绑定、置信度门槛及上文设置参与翻译指纹。

错位校验器 `segment_alignment` 为内置响应级校验器，通过 `validation.translation.validators` 显式启用。`validation.translation.alignment` 的 `decision_preset` 默认空字符串、`confidence_threshold` 和 `warning_threshold` 默认均为 0.8（后者为 0 时关闭不确定警告）、`tail_segments` 默认 3 且必须为正整数。宿主将拆分片段重组后，按请求顺序抽取尾部完整 Segment；证据为 `segments` 配对列表；其中原文和译文均去除 Ruby 标记及注音、保留正文，持久化文本和 finding 定位保持原样。问题按配对顺序命名，选项为 `aligned`、`misaligned`、`uncertain`。不携带历史上文，也不在超限时缩减抽查数量。错位 findings 为 `segment_misaligned`，不确定为不可修复的 advisory `segment_alignment_uncertain`；`matched_source` 记录抽查 Segment ID。两者都覆盖整批，具体保存和修复语义见 [翻译阶段](MINIMAL.md#53-翻译)。

术语插件将同一 Segment 的推荐译名缺失项合并请求，输出 required（需要术语修复）、
acceptable（无需修复，包括无关日常含义、正确译名及写法一致的合理简称）或 uncertain。请求同时携带本句全部命中术语作为只读参照，
只为缺失项提问；不同音译或近义名称不能仅凭语义相同判为 acceptable。无法可靠裁定的术语冲突使用 uncertain；
处理语义见 [翻译阶段](MINIMAL.md#53-翻译)。Run 的 `decision_validation` 保存判断、
Segment ID、证据摘要、耗时和用量；生成与校验用量合并汇总。人工保存译文只校验、不自动修复，
判断记录随结果保存。

### Decision 提示词声明

插件可通过 `PluginDescriptor.decision_prompts` 声明 `DecisionPromptDeclaration(validator_id, choice_ids, default)`；未声明的插件不提供编辑入口。声明必须属于本插件的校验器且不重复，固定选项为 2 至 255 个唯一非空 ID。`default` 提供一份默认内容，只含非空 `instructions` 和按固定 ID 填写非空描述的 `criteria`。宿主不允许用户增删选项。逐段校验器从 `context.decision_prompts[validator_id]` 取得运行时冻结内容，动态问题定位由校验器附加。

内置任务 `repetition_screening` 同样使用该编辑契约，固定选项为 `compare`、`skip`；该标识由宿主保留，不属于翻译校验器，也不受校验器启用配置控制。Decision 的判断说明和选项描述为完整可编辑内容，不追加 Prefix 或 Suffix，无语言限制。

Decision 提示词资源接口使用 `/api/v1/global/decision-prompts/{validator_id}` 与 `/api/v1/projects/{name}/decision-prompts/{validator_id}`：GET 读取有效内容，项目响应附 `global_sync`（available、same；全局无效时附 error），PUT 接收 `{content}`，项目 DELETE 接收 `{confirm: true}` 后验证全局内容并删除覆盖。`/api/v1/decision-prompts` 列举已加载声明。命名模板由 `/api/v1/decision-prompt-library/{validator_id}` 列举，其 `/{prompt_id}` 提供 GET、PUT `{content}` 和 DELETE。

`/api/v1/decision-prompts/{validator_id}/default` GET 读取声明默认内容，供用户显式加载草稿，便于修正无效覆盖。Run 在 `decision_prompts/{validator_id}.json` 保存 `{choice_ids, content}` 快照，加载时检查与已安装声明一致。

LLM 项目 Prompt 的 `/api/v1/projects/{name}/prompts/{stage}` GET 返回有效资源及 `inherited`。PUT 接收 `{language, content}` 并建立项目覆盖；DELETE 接收 `{language, confirm: true}`，验证全局资源后解除当前阶段、语言的覆盖。显式继承记录在项目元数据 `inherited_llm_prompts`，以 Prompt 文件名标识资源；普通文件缺失不视为继承。

LLM Prompt 的 `/api/v1/prompts/{stage}/default?language=...` GET 直接读取程序分发的原始 Prompt，组合预览也只使用内置资源，不读取用户全局覆盖。资源缺失时明确失败。

### 重复译文上下文比较

连续运行预检与启动增加 `include_repetition_consistency`、`apply_repetition_consistency`，均默认 `false`，并传递上述两个筛选布尔值。请求的 `stages` 保持原规范连续区间；启用后返回的执行步骤在翻译后或校对起点前包含 `repetition_consistency`，其动作通过 `run_actions` 指定。续作仅接受整项目 Run 并恢复原筛选开关。上游翻译尚需执行时，审查步骤 `count_deferred: true`，`selected: 0` 不表示没有候选；未完成 Run 可返回 `compatibility_deferred: true`，证据兼容性在到达该阶段时再次检查。

连续运行的 `apply_terminology_decision` 与 `apply_repetition_consistency` 在末尾步骤同样生效，默认 `false`；衔接后续阶段时必须授权自动应用，存在待审项时保留草案。一致性审查因上下文及超预算待审停止后，审查步骤状态为 `waiting_review`，保留 `run_id`，步骤 `summary.needs_review` 是待审出现数；连续任务摘要 `waiting_review: true`，后续步骤为 `not_executed`。该审查 Run 已成功生成草案，未应用，任务没有模型失败计数。连续任务累计实际调用用量，未执行步骤与零请求续作不新增未知用量。启用自动应用且无待审时应用限定本次 Run 的草案，保留正常应用和撤销契约。

重复审查预检与任务启动接收布尔值 `exclude_punctuation_only` 和 `screening_enabled`，分别默认 `true` 和 `false`；预检返回 `excluded_punctuation_groups`，Run 保存本次选择与排除数量。关闭初筛时 `decision_model` 为 `null`，不读取初筛 Prompt 或 Preset，也不保存其快照。

预检与启动支持 `run_action`：`resume` 续用、`decline` 结束旧 Run 并新建。预检的 `running_run` 沿用共享未完成 Run 描述，并包含已保存步骤数和总步骤数；`completed` 表示已完成审查的出现数，预检估算在续用模式下只统计未完成步骤。预检返回实际采用的 `only_file` 和两个筛选开关；续用模式恢复原值，启动时再次检查证据兼容性、当前执行指纹与 Run 身份。`running_run.execution_changed` 表示当前设置与最近一次执行不同，兼容时仍可显式 `resume`；超预算统计及剩余预算只针对尚未完成的工作。

支持续作的 Run 保存 `repetition_checkpoint: 2`、独立的 `repetition_protocol` 版本和 `repetition_input_fingerprint`。输入指纹不包含 Prompt、模型或预算状态；`stage_fingerprint` 与 continuation 指纹记录实际执行设置，包含 LLM Adapter hash。旧版未完成 Run 明确要求新建，不迁移检查点。

`repetition_checkpoints/{group_id}.json` 按组原子保存 `screening`、`confidence`、`screening_execution_index`，完成处置后增加 `result`（`proposals`、`keep`、`uncertain`）和 `comparison_execution_index`。来源索引从 1 开始：1 引用原始 Run，N 引用 `continuations/{N-1:04d}`；没有对应模型调用时为 null。初筛跳过结果引用初筛来源，超预算待审结果没有比较来源，续作时重新判定预算并保留已有初筛来源。检查点只保存结果和快照引用，不复制原文、上下文或资源正文。

原始执行及每次 continuation 使用共享快照装配保存实际 LLM 与 Decision 资源；术语证据在 Run 中保存一次。失败和取消保持 Run 为 `running`，未完成出现不计为失败，`last_interruption` 记录错误、步骤进度及可取得的请求 ID。Run 收集去重的预检、续作和客户端警告，实际 LLM 调用保存带执行来源索引的密钥审计；使用共享用量累计，零请求续作保留已有用量。

`repetition_consistency` 是独立 Prompt 资源，使用标准 Prefix、中段、Suffix 装配；模型连接沿用翻译 Preset。输入包含 `target_language`、请求内共享的 `context`、`terms`、`files`、`parts`，以及 `repetitions` 数组。每个重复组含稳定的短 `id`、`source`、`translations` 与 `occurrences`；出现短 ID 仅在所属重复组内有效，不得跨组引用。每次出现携带短 ID、File/part、位置、从零开始的译文索引、前后文引用和术语引用。`file` 是项目文件顺序中从 1 开始的索引，`files` 将本请求涉及的索引映射到文件名；`part` 是请求内 `parts` 表从 1 开始的索引，表中声明内容边界名称，每个 File/part 边界单独编号；`position` 是文件内从 0 开始的 Segment 序号，包含空片段。术语正文仅发送一次，命中的别名保存在各次出现的术语引用 `aliases` 中；术语引用的 `source_relations` 按顺序对应 `entity_knowledge.items`，关系以该次出现的局部上下文为基准。

响应为单个 JSON 对象，仅含 `repetitions` 数组，每个输入重复组恰好对应一个结果，含原样组 `id`、`groups`、`keep`、`uncertain`。每个统一子组含 `members`、`target`、`apply`、`reason`；成员至少两个，`target` 必须是组内成员并选择其现有译文，`apply` 是 `members` 的无重复子集，可以为空，`reason` 使用目标语言。各次出现的短 ID 必须完整、不重复地划入子组或保留、待审列表。该协议不输出 JSONL 或结束记录。

草案审核选择以建议 ID 索引，含 `accepted`、`target` 及可选 `apply`；应用成员遵守相同子集规则。未提供 `apply` 时使用建议默认值；已有建议缺少该字段时使用全部成员。初筛跳过组由读取接口提供默认关闭的人工建议，ID 为 `{group_id}-manual`，成员为该重复组全部出现，选择仍保存于 Run；不可变草案正文保持原样。
