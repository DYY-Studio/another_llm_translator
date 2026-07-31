# Adapter 契约

本文区分已经实现的契约与 provisional 设计。实现行为以测试和
`docs/MINIMAL.md` 为准。

## 1. 声明式 JSON LLM Adapter（已实现）

全局定义位于 `llm_adapters/<adapter_id>.json`。LLM Preset 引用 Adapter ID；
项目实时引用 Preset，但必须显式拥有对应 Adapter 的项目副本。每个 Run 保存
实际使用的 Adapter 与 Preset 快照。

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

### 请求边界

- 请求地址只由当前 Preset 的 `base_url` 与 `endpoint` 组成。
- 声明式 Adapter 固定构建非流式 JSON POST；HTTP Client、代理、超时、限速、
  重试、取消和日志由宿主负责。
- `body` 是完整模板。可直接加入 `reasoning_effort`、`response_format` 或
  Provider 自定义嵌套字段，不存在与宿主默认字段合并的覆盖顺序。
- body 支持 `${model}`、`${messages}`、`${temperature}`、
  `${max_output_tokens}`、`${stream}`。占位符必须独占一个 JSON 字符串值，
  替换后保留数组、数字和布尔类型。
- Header 可使用上述占位符及 `${api_key}`；嵌入字符串时结果为字符串。
- `${api_key}` 禁止出现在 body；URL 不支持模板，因此密钥也不能进入 URL。
- 未知字段、未知占位符、混合 body 文本占位符和非法 schema 立即失败。

### 响应边界

`response_content_pointer` 是 RFC 6901 JSON Pointer。路径不存在或结果不是
字符串时，当前请求失败；不猜测备用字段。提取出的字符串继续进入统一 JSONL
解析、思考标签处理、格式修正和业务校验。

常规 HTTP 状态与网络异常不由 Adapter 分类。需要特殊签名、非 JSON body、
非 JSON 成功响应或特殊错误解析的端点超出 schema 1 范围。

### 指纹与密钥

阶段指纹包含 Adapter ID、项目定义内容 Hash、Preset ID 和 Preset 内容 Hash。
项目 Adapter 副本与 Run 快照保存定义原文，但不解析或保存环境变量中的 API
Key。调试请求记录仍经过敏感 Header 清理。

## 2. Document Adapter（Beta）

Document Adapter 是同一格式的导入与导出边界。当前内置 `txt` 与 `epub`：

```python
class DocumentAdapter(Protocol):
    adapter_id: str
    version: str
    capabilities: frozenset[str]

    def import_sources(...) -> DocumentImport: ...
    def export_sources(...) -> list[Path]: ...
```

能力名为 `import`、`translated_export` 和 `bilingual_export`。宿主在调用前
检查所需能力，不支持时明确失败。

### 导入

Adapter 返回有序 `ImportedFile`，每项包含原始文件位置、展示名称、Segment
文本和输入编码信息。宿主负责：

- 分配 File ID、Segment ID 和行序；
- 复制原始输入；
- 以临时项目目录完成事务化初始化；
- 保存通用 File/Segment 记录。

Adapter 可同时返回 JSON 可序列化的 `opaque_state`。宿主将其保存在
`source/adapters/<adapter_id>/state.json`，只校验归属、版本和完整性，不解释
内部字段。

### 导出

宿主选择阶段结果、执行缺失结果规则和前导空白恢复，再向原 Adapter 提供 File、
Segment、目标文本、模式和不透明状态。Adapter 只能在给定 staging 目录生成
相对路径；全部生成成功后，宿主逐文件原子移动到正式输出目录。

Adapter 缺失、版本不一致、状态损坏、能力不足或运行异常都会终止当前操作。
不会自动改用 TXT，也不会删除仍可读取的项目 Segment 和阶段结果。

### EPUB 0.1

EPUB 项目每次只导入一个 `.epub`。Adapter 保存原始容器，并记录 OPF、spine
顺序以及 Segment 到 XHTML `text`/`tail` 槽位的定位。导出只重写被翻译的
XHTML，原样复制导航、元数据、图片、CSS、字体和其他资源。

双语模式在同一 XHTML 文本槽中按“源文、换行、目标文本”写入，并在 body
声明 `white-space: pre-line`。该规则属于 EPUB Adapter，不是宿主通用排版树。

安全边界拒绝：

- ZIP 绝对路径、`..`、反斜杠路径、重复路径和符号链接；
- 过多条目、过大解压总量和异常压缩比；
- 越界或缺失的 OPF/spine 资源；
- XML DTD、实体声明和非法 XML。

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
        protocol_version=1,
        document_adapters=(MyDocumentAdapter(),),
    )
```

宿主拒绝重复/空插件 ID、重复/空 Adapter ID、未知协议版本和不完整 Adapter
描述。插件代码与宿主同进程运行，拥有当前进程权限；安装即表示信任。插件不得
自行操作 Run、限速器、项目 JSONL 或正式输出目录。

插件专属配置的命名空间与验证接口尚未实现；当前 Document Adapter 只接收现有
项目配置。不要依赖未文档化的内部对象。

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
端点限速等连接设置。项目引用 Preset ID；Run 保存实际解析的 Preset 快照，
阶段指纹包含 Preset ID 和定义内容 Hash。

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

Preset 修改立即影响所有引用项目，不维护版本历史。项目缺少其引用的 Adapter
时明确失败；Web 只在用户操作后复制全局 Adapter，不自动补齐或改用其他
Preset。旧内联连接配置仍可读取，并可通过 Web 明确保存为 Preset 后切换；该
兼容路径不扩张新字段。
