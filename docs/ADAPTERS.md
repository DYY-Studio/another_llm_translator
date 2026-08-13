# Adapter 契约

本文区分已经实现的契约与 provisional 设计。实现行为以测试和
`docs/MINIMAL.md` 为准。

## 1. 声明式 JSON LLM Adapter（已实现）

全局定义位于 `llm_adapters/<adapter_id>.json`。LLM Preset 引用 Adapter ID；
项目实时引用全局 Adapter，不保存项目副本。每个 Run 保存实际使用的 Adapter
与 Preset 快照。

最小定义：

```json
{
  "schema_version": 1,
  "adapter_id": "openai-compatible",
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
  "response_reasoning_content_pointer": "/choices/0/message/reasoning_content"
}
```

该字段可省略；内置 OpenAI-compatible 定义已配置上述路径，并兼容不返回该
扩展字段的合法响应。

需要把规范化的 system/user/assistant 消息转换为 Provider 原生形状时，可设置
`messages_format`（可选，默认 `openai` 原样透传）：

- `anthropic`：system 消息剥离到顶层字段，其余 user/assistant 消息保留
  字符串 content；
- `gemini`：system 消息剥离到顶层字段，user/assistant 映射为 `contents`
  数组的 `role: "user"/"model"` 与 `parts: [{"text": ...}]`。

`body` 模板可使用 `${system}` 占位符读取剥离后的 system 文本（多条消息以
空行拼接），例如 Anthropic 的顶层 `system` 与 Gemini 的 `system_instruction`。
该占位符对 `openai` 格式同样可渲染，但 system 消息仍保留在 `${messages}`
中，通常不应同时使用。

### 请求边界

- 请求地址只由当前 Preset 的 `base_url` 与 `endpoint` 组成。API 版本前缀
  （如 `/v1`、`/v1beta`）写入 `base_url`，`endpoint` 必须是相对路径。
- Preset `endpoint` 允许且只允许 `${model}` 占位符（如 Gemini 的
  `/models/${model}:generateContent`），请求时由宿主替换为模型名；
  其他占位符立即失败。
- 声明式 Adapter 固定构建非流式 JSON POST；HTTP Client、代理、超时、限速、
  重试、取消和日志由宿主负责。
- `body` 是完整模板。可直接加入 `reasoning_effort`、`response_format` 或
  Provider 自定义嵌套字段，不存在与宿主默认字段合并的覆盖顺序。
- body 支持 `${model}`、`${system}`、`${messages}`、`${temperature}`、
  `${max_output_tokens}`、`${stream}`。占位符必须独占一个 JSON 字符串值，
  替换后保留数组、数字和布尔类型。
- Header 可使用上述占位符及 `${api_key}`；嵌入字符串时结果为字符串。
- `${api_key}` 禁止出现在 body；URL 不支持模板，因此密钥也不能进入 URL。
- 未知字段、未知占位符、混合 body 文本占位符和非法 schema 立即失败。

### 响应边界

`response_content_pointer` 是必需的 RFC 6901 JSON Pointer，结果必须是字符串。
JSON Pointer 的数组索引 token 支持负索引 `-N`（RFC 6901 扩展）：`-1` 为
最后一个元素、`-2` 为倒数第二。当思考块总是排在最前、文本块在最后时
（Anthropic `content`、Gemini `parts`），负索引可稳定取到最后文本块。
越界、空数组与普通缺失路径同样快速失败。
可选的 `response_reasoning_content_pointer` 结果必须是字符串或 null。推理路径
不存在时规范化为 null；字段存在但类型错误时当前请求失败，不猜测备用字段。

Adapter 规范化返回 `content` 和可空的 `reasoning_content`。宿主随后按统一
严格规则从 content 开头剥离一个完整已知思考 Tag；若结构化字段与内嵌块同时
非空则快速失败，不猜测拼接顺序。`reasoning_content` 只存在于当前请求生命
周期，不进入阶段记录。debug 模式只保留原始响应，不新增思考副本。

常规 HTTP 状态与网络异常不由 Adapter 分类。需要特殊签名、非 JSON body、
非 JSON 成功响应或特殊错误解析的端点超出 schema 1 范围。

### 指纹与密钥

阶段指纹包含 Adapter ID、全局定义内容 Hash、Preset ID 和 Preset 内容 Hash。
Run 快照保存定义原文，但不解析或保存环境变量中的 API Key。调试请求记录
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
  使用草稿中的 Adapter、Base URL、API Key 环境变量名、代理和超时，但不保存
  草稿。路径 Preset ID 与草稿 ID 不一致时立即拒绝。
- Preset 编辑器保留可手工输入的模型 ID；发现结果在字段下方按名称或 ID
  本地搜索。选择结果只更新当前草稿并收起列表，必须显式保存才会生效。
- `response_models_pointer` 指向模型条目数组；`response_model_id` 是条目内
  必填键名；`response_model_display` 与 `response_model_strip_prefix` 可选。
- 展示名缺失时回退为模型 ID；`response_model_strip_prefix` 从模型 ID 前缀
  剥离（如 Gemini 的 `models/`）。条目缺少 ID 或响应形状非法时快速失败。
- 缺少 `models` 规格、缺失 API Key 环境变量、HTTP 错误或网络异常都明确
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

- 三个指针均可选；任一成功响应的已声明指针缺失或值非法（非非负整数）时，
  整个任务标记为「usage 不可用」且公开计数归零，不展示部分合计，也不使用
  本地启发式估算冒充端点账单。
- 宿主在每个成功请求后累计规范化计数，任务结束时写入任务摘要与 Run
  `manifest.json` 的 `usage` 字段。Adapter 未声明 `usage` 时任务与 Run
  摘要不包含 usage 字段。
- 主请求的限速、重试与错误语义不受影响。

### 内置 Adapter 定义

- `openai-compatible`：Bearer API Key，Chat Completions body，正文 pointer
  `/choices/0/message/content`，推理 pointer
  `/choices/0/message/reasoning_content`。
- `anthropic`：`x-api-key` 与 `anthropic-version: 2023-06-01`，body 顶层
  `system`，pointer `/content/-1/text`。未启用 thinking 时 content 首块即
  文本；负索引使 `extra_body` 日后启用 thinking 时仍可稳定取到最后文本块。
  不配置 reasoning 指针；需要思考正文时可复制定义并设
  `/content/-2/thinking`。未启用 thinking 或思考块缺失时结果为 null；字段
  存在但不是字符串或 null 时快速失败。
- `google-gemini`：`x-goog-api-key`（密钥不进入 URL），model 由 Preset
  `endpoint` 的 `${model}` 占位符进入请求路径，pointer
  `/candidates/0/content/parts/-1/text`。不内置 thinkingConfig，思考模型
  默认思考开启时 text 块仍恒为最后一个 part。不配置 reasoning 指针；可自配
  `/candidates/0/content/parts/-2/text`，缺失路径结果为 null，字段存在但不是
  字符串或 null 时快速失败。
- `openai-responses`：`input` 原样接收规范化消息（system/user/assistant），
  body 含 `"store": false`，pointer `/output/-1/content/-1/text`。宿主直接
  解析 REST JSON，不使用 SDK 才提供的 `output_text` 便利属性；当前请求不声明
  tool，因此最终 message 是最后一个 output，正文是其最后一个 content。响应
  缺少该结构时快速失败。

三个新定义都只存在于全局目录；示例 Preset 见
`llm_presets/anthropic-claude.json`、`google-gemini.json` 与
`openai-responses.json`。四个内置定义均声明 `models` 与 `usage` 映射：
Anthropic 无 total 计数，Gemini 的模型 ID 经 `models/` 前缀剥离。所有内置
Adapter 的 `models` 端点与示例 Preset 的 `endpoint` 都是不含版本前缀的相对
路径；版本前缀（`/v1`、`/v1beta`）必须写在 Preset `base_url` 中。

## 2. Document Adapter（Beta）

Document Adapter 是同一格式的导入与导出边界。当前内置 `txt` 与 `epub`：

```python
class DocumentAdapter(Protocol):
    adapter_id: str
    version: str
    capabilities: frozenset[str]
    extensions: frozenset[str]
    import_options: tuple[DocumentChoiceOption, ...]
    run_options: tuple[DocumentChoiceOption, ...]

    def import_sources(...) -> DocumentImport: ...
    def export_sources(...) -> list[Path]: ...
```

`export_sources` 还会收到宿主项目配置中的 `target_language: str` 和
`target_language_tag: str`。前者是供模型和人阅读的自由文本名称，后者是可选的
BCP 47 输出语言标签；两者职责分离。Adapter 可以忽略、应用到自己的格式元数据，
或在标签为空时明确拒绝导出。宿主不按 Adapter ID 推断语言行为。更新该导出参数
后，Document Adapter 插件协议版本为 `7`；旧协议插件会快速失败。

能力名为 `import`、`translated_export` 和 `bilingual_export`。宿主在调用前
检查所需能力，不支持时明确失败。

可导入 Adapter 必须声明至少一个小写、带前导点的扩展名。宿主按大小写不敏感
匹配扩展名；不同 Adapter 声明同一扩展名时插件加载直接失败，不猜测格式。
内置 TXT 声明 `.txt`、`.text`，EPUB 声明 `.epub`。

### 导入

Adapter 返回有序 `ImportedFile`，每项包含原始文件位置、展示名称、Segment
净文本和输入编码信息；可选的 `model_sources` 与 Segment 一一对应，仅用于模型
看到的格式契约。宿主负责：

- 分配 File ID、Segment ID 和行序；
- 复制原始输入；
- 以临时项目目录完成事务化初始化；
- 保存通用 File/Segment 记录。

`ImportedFile` 可选返回与 `segments` 一一对应的 `segment_part_ids`。省略时宿主
将所有 Segment 归入 `document`；提供时每项必须是非空字符串。宿主把该值写入
Segment 的 `part_id`，并以 `(file_id, part_id)` 限制 Chunk、LLM 请求和参考上下文。
这不会改变 File 的存储、选择、调度或导出边界；旧项目缺少有效 `part_id` 时要求
重新创建，不从 locator 推测或迁移。

每个 `ImportedFile` 可携带 JSON 可序列化的 `opaque_state`。宿主将其保存在
`source/adapters/<adapter_id>/<file_id>.json`，并在 File 记录中保存 Adapter
ID、版本和状态位置；宿主只校验归属、版本和完整性，不解释内部字段。

Adapter 可声明由固定字符串选项组成的 `import_options` 和类型相同的
`run_options`。宿主展示声明并校验取值；导入选项只在导入调用中传入，运行选项
由 Adapter 固化在 File 的 `opaque_state`。Adapter ID 与版本进入阶段指纹
（内置 EPUB 的既有运行选项值也纳入）。修改选项不会改写既有
Segment，必须移除并重新导入文件。不支持自由键值或嵌套选项。

CLI 的 `init` 与 `files-add` 用可重复的
`--adapter-option ADAPTER.OPTION=VALUE` 传入选项（如
`--adapter-option epub.ruby_mode=aozora`）；Web 上传使用同名
`adapter_options` JSON。两者构建同一形状，取值语义统一在宿主
`validate_document_import_options` 边界校验。

### 契约测试

`tests/test_document_adapter_contract.py` 是外部 Document Adapter 的契约
基准：它用一个独立的第三方风格 Adapter（`record`，`.rec`）走通全部宿主路径
——按扩展名与显式 ID 导入、选项校验与透传、`opaque_state` 存储往返、
`part_id`/`model_source` 落地、翻译时 `normalize_model_output` 应用、双语与
纯译文导出、运行选项固化生效，以及 Adapter 缺失、版本不匹配、状态损坏、
能力不足和指纹跟踪。任何标准第三方 Adapter 必须通过该套件的通用路径。

### 版本与升级策略

Adapter 版本字符串必须与 File 记录严格相等才能导出，不匹配立即失败。Adapter
升级或选项变更的方式是移除文件并重新导入：宿主不解释、不迁移 `opaque_state`，
也不会改写既有 Segment 和阶段结果；指纹变化使旧结果不再复用。兼容范围
（如 semver 前缀）语义不在当前协议中，等待至少一个真实第二实现出现后由用户
决策引入；协议版本不匹配的插件直接快速失败。

### 导出

宿主选择阶段结果、执行缺失结果规则和前导空白恢复，再逐 File 向来源 Adapter
提供该 File、Segment、目标文本、模式和不透明状态。Adapter 只能在给定 staging
目录生成相对路径；全部生成并验证成功后，宿主逐文件移动到正式输出目录。

Document Adapter 插件协议当前为版本 7。统一 TXT 导出由宿主改用内置 `txt`
Adapter 处理各 File，不调用来源 Adapter，也不解释来源格式状态。

Adapter 缺失、版本不一致、状态损坏、能力不足或运行异常都会终止当前操作。
不会自动改用 TXT，也不会删除仍可读取的项目 Segment 和阶段结果。

### EPUB 0.3

EPUB Adapter 每次导入一个 `.epub`；同一项目可包含多个 EPUB File。Adapter
保存各 File 的原始容器，并记录 OPF、spine
顺序以及 Segment 到 XHTML 文本流和 `text`/`tail` 槽位的定位。每个 spine XHTML
的归档路径作为 Segment 的 `part_id`；普通透明内联
元素中的相邻槽合并为一个复合 Segment；未知结构和 `br` 形成边界。导出只重写
被翻译的 XHTML，原样复制导航、元数据、图片、CSS、字体和其他资源。

导出时宿主提供 `target_language` 和可选的 `target_language_tag`。EPUB Adapter
要求语言标签为非空 BCP 47 标签，并要求目标语言名称非空。单语输出的 OPF
`dc:language` 设为该标签；双语输出把该标签放在第一项，随后保留源语言。已重写
的 spine XHTML 同时更新根元素的 `lang` 和 `xml:lang`。中文应使用 `zh-Hans` 或
`zh-Hant`，以便 Apple Books 识别正确的语言和字体。

译文和双语输出会生成基于项目、File、目标语言标签和输出模式的稳定独立出版标识，
并将 OPF 主标题分别后缀为 `（目标语言）` 和 `（目标语言·双语）`。同一输出再次
导出时标识保持不变；每次导出会刷新 EPUB 3 的 `dcterms:modified`，或 EPUB 2 的
`dc:date opf:event="modification"`，用于阅读器缓存更新。源书原有标识和其他书籍
元数据保留不变。

双语模式在单槽 Segment 中按“源文、换行、目标文本”写入；复合 Segment 保留
所有源槽，并在最后一个槽后追加“换行、目标文本”。body 声明
`white-space: pre-line`。该规则属于 EPUB Adapter，不是宿主通用排版树。

EPUB Adapter 只接受 OPF `package` 版本 `2.0` 或 `3.0`。EPUB 3 XHTML 可无
DOCTYPE，或使用无外部标识的 `<!DOCTYPE html>`；EPUB 2 XHTML 可无 DOCTYPE，
或使用 PUBLIC `-//W3C//DTD XHTML 1.1//EN`，SYSTEM 地址只作为声明数据而不
会被加载。所有外部 DTD、实体声明、版本不匹配的声明、SYSTEM-only 和错误
PUBLIC 标识都会快速失败。

普通透明内联元素中的相邻文本槽构成一个复合 Segment；纯译文把整条译文写入
首槽并清空其余槽，保留标签及 attrs 骨架，不猜测局部格式对应关系。双语导出
保留源槽并在末槽后写入译文。Ruby 是同一文本流中的内联成员；包含 Ruby 的
复合 locator 可以按源文顺序混合普通 `text`/`tail` 槽和 Ruby 槽，只有没有相邻
文本的独立 Ruby 才继续使用旧的 `kind: "ruby"` 形状。导入可选择
`aozora`（默认，`｜原文《Ruby》`）、`base_only` 或 `parenthetical`
（`原文（Ruby）`）。无法确定基础文字和读音的嵌套或残缺结构会带 XHTML
位置快速失败。纯译文导出把整条译文写入混合 Segment 的首个可用位置，清空其余
普通槽并删除该 Segment 内全部 Ruby；双语导出保留完整源句和 Ruby，并只在整个
Segment 末尾追加普通译文。`ruby_mode=aozora` 时，模型可以省略译文、校对或润色
结果中的 Ruby 标记和 reading，但必须翻译属于正文的 base，不能因其位于 Ruby 中
而照抄。保留时须返回严格闭合的 `｜已翻译base《目标语言适用reading》`，系统会在
纯译文和双语译文区域恢复 EPUB Ruby；reading 必须翻译或转写，无法适配时应去掉
标记和 reading，仅返回已翻译 base。没有返回 Ruby 不会触发重试；不完整、嵌套、
含 HTML 或跨行的形式按普通文本保留。
`base_only` 和 `parenthetical` 不执行 Ruby 还原。
确定性术语注入使用同一严格青空语法：匹配视图分别保留 base 正文和 reading，
不把二者拼接。base 可跨相邻 Ruby 匹配连续正文，直接相邻 Ruby 的 reading 也会
连续组合，普通正文会切断 reading 组合。因此 `｜漢《かん》｜字《じ》` 可命中
“漢字”和“かんじ”，而 `｜漢《かん》A｜字《じ》` 不会把 reading 拼成“かんじ”；
同一术语从两个视图命中时只注入一次。该匹配规则不改写 Segment 原文或发送给
模型的 `source`。

当 `inline_format_mode=markers` 时，EPUB 另保存 `model_source`，把符合
`inline_format_policy` 的普通内联标签转换为无 attrs 的唯一成对标记；`plain` 是
默认值，模型只看到净文本。`tiered` 要求语义关键标签保留，表现层标签可整体省略；
`strict` 要求全部源标签保留。宿主把受控标记校验交给 EPUB Adapter：未知、重复、
未闭合、错误嵌套或破坏父子关系的结果进入既有格式修复预算，耗尽后 Segment 失败。
纯译文仍使用原标签和 attrs 的空骨架写回，模型标记不会作为 HTML 直接写入 EPUB；
详情界面同时显示净文本与模型文本预览。

安全边界拒绝：

- ZIP 绝对路径、`..`、反斜杠路径、重复路径和符号链接；
- 过多条目、过大解压总量和异常压缩比；
- 越界或缺失的 OPF/spine 资源；
- 非法版本化 DOCTYPE、外部 DTD、实体声明和非法 XML。

## 3. 可信 Python 插件宿主（Beta）

插件包在 entry-point 组 `minimal_llm_translator.plugins` 注册一个
`PluginDescriptor` 实例或返回该实例的无参函数：

```toml
[project.entry-points."minimal_llm_translator.plugins"]
my_plugin = "my_package.plugin:descriptor"
```

```python
from app.plugins import PluginDescriptor

def descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="my-documents",
        version="1.0.0",
        protocol_version=7,
        document_adapters=(MyDocumentAdapter(),),
    )
```

宿主拒绝重复/空插件 ID、重复/空 Adapter ID、未知协议版本和不完整 Adapter
描述。插件代码与宿主同进程运行，拥有当前进程权限；安装即表示信任。插件不得
自行操作 Run、限速器、项目 JSONL 或正式输出目录。

导入选项只支持上述类型明确的单层 choice 声明；插件不得把它当作通用配置或
运行期设置。不要依赖未文档化的内部对象。

## 4. Python LLM Adapter（provisional，未实现）

未来 Python LLM Adapter 仍只负责：

- 将规范化 model/messages/temperature/max output/stream 转成 HTTP 请求规格；
- 将宿主取得的响应转成 content 或规范错误类别。

它不得自行创建 HTTP Client、发送请求、限速、重试、写 Run 或读取密钥存储。
具体 Python 方法签名、错误类型和配置接口必须等待第一个 JSON 模板无法支持的
真实端点，再与第二个实现共同验证；当前不提供动态加载器或兼容承诺。

## 5. LLM Preset（已实现）

Preset 位于全局 `llm_presets/<preset_id>.json`，实时引用一个 Adapter ID，
并保存端点、模型、鉴权环境变量名、模型 Token 能力和
端点限速等连接设置。项目配置一个全局 Preset，并可为术语、翻译、校对和润色
分别选择覆盖；空覆盖使用全局 Preset。Run 保存当前阶段实际解析的 Preset
快照，阶段指纹包含该 Preset ID 和定义内容 Hash。

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

Preset 修改立即影响所有引用项目，不维护版本历史。Adapter 修改立即影响
所有引用 Preset。项目缺少其引用的全局 Adapter 时明确失败，不自动补齐或
改用其他 Preset。项目、全局模板和 Run 续作只接受命名 Preset，不支持内联
连接配置。

### 后期能力（未实现）

路线 Stage 10 的 `models` 请求与响应映射、规范化 `usage` 映射和任务内
汇总已实现（见 §1）。模型发现只由用户手动触发，缺少 usage 时明确显示
不可用。

路线 Stage 11 才考虑单 Preset 多 API Key。Preset 仍只记录密钥环境变量名；
每 Key 限流、调度、失效恢复和 Run 审计必须先形成可测试规则。该能力不提供
Provider fallback，也不静默吞掉鉴权或配额错误。
