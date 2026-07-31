# 产品演进路线图

本路线以已经通过测试的行为为起点。每个 Stage 独立设计、实现、测试和提交，
不为后续阶段提前建设抽象。`docs/MINIMAL.md` 只描述已经实现的行为；本文描述
未来方向和进入条件。

当前项目目录继续作为完整项目边界。SQLite 迁移前不建设 Repository 层或双写
路径；不增加自动 Provider 判断、静默 fallback、任意格式互转或未经真实需求
验证的插件能力。

## 当前基线

- TXT/EPUB、File/Segment/Chunk/Run、项目 JSONL、CLI 和完整翻译流程已经实现。
- CLI、本地 Web 和开发编辑器共享阶段、Run、限速、恢复和项目持久化代码。
- 本地 Web Alpha 已覆盖项目、术语、结果审校、阶段决策、apply 和 export，并
  保持回环地址和单写任务安全边界。
- 产品路线 Stage 1 至 Stage 8 已完成；下一阶段是 SQLite 标准项目存储。

## Stage 1：项目文件生命周期（已完成）

- 支持创建和打开 0 文件 TXT/EPUB 项目。
- TXT 项目可追加和移除多个文件；EPUB 保持单文件。
- File/Segment ID 删除后不复用。
- 空项目和纯空白项目禁止执行 LLM、apply 和 export。
- 被移除 Segment 的历史结果保留，但不再参与统计、复用和导出。
- CLI 与 Web 文件管理均已实现。

## Stage 2：结构化项目设置（已完成）

Web 改用分组表单编辑当前全部项目配置，不再要求用户直接修改完整 TOML。
LLM Adapter JSON 继续使用独立高级编辑器。

- 服务端接收类型化配置对象，执行现有严格校验后写回规范 TOML。
- 非法类型或字段组合整体拒绝，保存失败不得破坏原配置。
- CLI 配置读取、阶段指纹和项目目录结构保持不变。
- 本阶段不加入 Preset、分阶段模型或全局设置。

全部配置均可通过分组表单读写。服务端以类型化对象作为 Web 边界，复用严格
配置校验并原子写入规范 TOML；非法输入不会破坏原配置。桌面和移动端浏览器
QA 已纳入本阶段验收。

## Stage 3：全局设置、Prompt 与实时 LLM Preset（已完成）

Web 增加全局配置、全局 Prompt 和命名 Preset 管理。全局配置和 Prompt 修改
只影响新项目或显式同步的项目。

Preset 使用独立 JSON 文件。项目保存 Preset ID 并实时引用，不复制连接参数；
Preset 修改会立即影响引用项目。Run 启动时保存实际解析的 Preset 快照，阶段
指纹包含 Preset ID 和完整内容 Hash。Preset 缺失、Adapter 缺失或配置非法时
快速失败，不改用其他 Preset。Preset 不做版本管理，历史审计由 Run 快照保证。

Preset 固定包含：

- Adapter ID、base URL、endpoint、model；
- API Key 环境变量名和 proxy；
- context window、max output、安全余量和 token safety factor；
- RPM、ITPM、并发数和请求超时；
- `extra_body` 自定义附加 JSON 对象。

温度、Chunk、Prompt、重试、校验和调度策略继续属于项目或阶段。

### Preset `extra_body`

- 只附加到请求 JSON body，不影响 Header、URL 或响应解析。
- 支持嵌套对象和数组，例如 OpenRouter 的 `provider.order`。
- 宿主先渲染 Adapter 的完整 body，再加入 `extra_body` 的顶层字段。
- 与 Adapter body 出现同名顶层字段时快速失败；不覆盖、不递归合并。
- 修改 Adapter 固有字段应编辑或复制 Adapter，不通过 Preset 覆盖。
- 不支持模板占位符，尤其禁止 `${api_key}`。内容会明文进入诊断预览、Run
  快照和阶段指纹，因此不得保存密钥。
- 请求预览显示最终合并 body，并继续执行 Header 脱敏。
- 非对象、非法 JSON、字段冲突必须在创建 Run 或发送请求前失败。

修改 `extra_body` 会产生新阶段指纹，并进入现有混合指纹确认流程。验收覆盖
嵌套对象、数组、OpenRouter provider order、冲突和占位符拒绝、请求预览、
Run 快照及指纹变化。

Adapter 定义继续保存在项目中；Preset 只引用 Adapter ID。选择项目尚未拥有
的 Adapter 时必须显式复制全局定义。项目、全局模板和 Run 续作均只接受
Preset；内联 LLM 连接配置不再支持。

## Stage 4：规范化 LLM 思考响应（已完成）

- LLM Adapter 规范化返回 `content` 和可空的 `reasoning_content`。
- content 前导思考标签被剥离后，正文与思考正文分别进入两个字段。
- JSON Adapter 可声明可选的结构化思考字段 JSON Pointer。
- 结构化字段与内嵌思考块同时非空时快速失败，不猜测合并顺序。
- `reasoning_content` 只存在于请求生命周期，不进入 Segment、阶段结果或普通
  Run 状态。
- debug 模式继续保存原始响应，不额外复制结构化思考记录。
- 本阶段不设计展示、开关、分析工具或思考持久化。

## Stage 5：File 级 Document Adapter 与多格式项目（已完成）

- 将 Adapter ID、版本和不透明状态从项目级下沉到 File 级。
- 迁移现有项目时把项目级 Adapter 信息写入每个活动 File，Segment ID 和历史
  结果保持不变。
- 空项目不再预先锁定格式，可追加不同受支持格式的文件。
- Document Adapter 以单个逻辑 File 为导入、状态和导出边界。
- 原格式导出逐 File 调用来源 Adapter。
- 宿主提供统一 TXT 导出，可把任意 File 的有序 Segment 输出为 TXT。
- 除来源原格式和 TXT 外不支持格式转换。
- Adapter 缺失、版本不兼容或 File 状态损坏时明确报告受影响文件，不静默
  转为 TXT。
- 不建立通用 DOM、排版树或跨格式中间文档模型。

## Stage 6：按文件筛选导出（已完成）

- CLI export 增加 File ID 范围选择。
- Web 导出页使用经典 Ctrl/Cmd/Shift 多选。
- 筛选同时适用于原格式和 TXT 导出；未选择时导出全部活动文件。
- 未知 File ID、空选择、输出路径冲突或所选文件缺少结果时，在写输出前整体
  失败。
- 不增加按 Segment 导出或跨文件合并。

## Stage 7：四阶段独立 LLM Preset（已完成）

- 项目设置一个全局 Preset，术语、翻译、校对和润色可分别选择覆盖 Preset。
- 未设置阶段覆盖时使用全局 Preset，不增加其他继承层。
- 每个阶段独立解析 Adapter、端点、模型、`extra_body`、Token 限制和限速。
- `run-all` 按实际 Preset 复用 HTTP Client；不同 Preset 使用独立限速窗口，
  相同 Preset 共享窗口。
- Run 快照和阶段指纹保存各阶段实际解析结果。
- Preset 或 `extra_body` 变化继续触发现有混合指纹确认，不自动重做结果。

## Stage 8：外部项目位置（已完成）

- CLI 支持在明确目录创建项目，并继续允许通过绝对路径打开。
- Web 支持输入并打开现有项目目录，以及在指定父目录创建项目。
- 保存仅属于本机 Web 的最近项目路径列表；不递归扫描磁盘，不自动移动项目。
- 路径规范化后去重；无效项目、权限不足和路径冲突快速失败。
- 默认 `projects/` 目录继续可用。
- 项目保持可整体移动的自包含目录，为后续 `project.sqlite` 保持稳定边界。

## Stage 9：SQLite 标准项目存储

进入条件：多格式 File 状态、阶段结果、Preset 和外部项目位置语义已经稳定。

- `project.sqlite` 保存项目元数据、File、Segment、术语、阶段结果和 Run 索引。
- 原始输入、Adapter 状态、配置、Prompt、Preset/Adapter 快照、调试 Payload
  和输出继续作为外围文件。
- 使用单写事务、外键、唯一约束和必要索引。
- 首版优先使用标准库 `sqlite3`，根据真实性能测试决定是否增加其他库；不引入
  ORM。
- 显式迁移命令先创建数据库并校验数量和引用，再原子切换项目格式。
- 不进行 JSONL/SQLite 双写。旧数据保存在可恢复备份目录，迁移失败时原项目
  仍可使用。
- 完成迁移后删除 JSONL 运行路径和无用兼容代码。

## Stage 10：Adapter 模型发现与 usage 统计

- Adapter 可选声明 models 请求规格和响应映射；宿主继续负责鉴权、代理、
  超时和 HTTP 生命周期。
- Web 只在用户手动触发时检测连通性并读取模型列表，用于填写 Preset；不自动
  判断 Provider、选择模型或切换端点。
- Adapter 可选把响应中的 usage 映射为规范化计数；宿主汇总一次任务内端点
  实际返回的消耗，并在任务和 Run 摘要中展示。
- Provider 未返回 usage 时明确显示不可用，不使用本地启发式估算冒充端点
  账单或实际消耗。

模型发现和 usage 都是 Adapter 对端点响应的可选映射，待出现真实端点差异后
共同设计；本阶段不改变主请求的限速或重试语义。

## Stage 11：单 Preset 多 API Key

- Preset 只保存多个 API Key 环境变量名，不保存密钥值。
- 实现前必须明确每 Key 与端点的限流范围、调度策略、失效状态、恢复规则和
  Run 审计行为。
- Key 轮换不得扩展成 Provider fallback，也不得静默掩盖鉴权或配额错误。

该能力涉及限流状态和费用归属，必须在 Stage 10 的端点观测能力稳定后单独
设计和验收。

## Stage 12：国际化与本地化

这是最低优先级阶段，以中文和英文两个真实语言作为首次实现，不提前建设只有
单语言消费者的翻译框架。

- 前端抽取消息目录、日期数字格式和可访问性文本。
- CLI 使用稳定消息键或标准库本地化机制。
- Web API 在确有前端消费者时增加稳定错误代码与参数。
- 项目内容、Prompt、目标语言和模型输出不随 UI 语言自动变化。
- 不实现自动翻译文案、远程语言包或插件本地化市场。

## 协议成熟度

产品 Stage 与协议稳定性分别推进，不能因为某项进入产品路线就提前冻结 SDK。

### 声明式 JSON LLM Adapter（已实现）

当前 schema 已覆盖类型化占位符、任意嵌套 body、自定义认证 Header 和响应
JSON Pointer。HTTP、限速、重试、取消、Run 收尾和调试记录始终由宿主管理。
API Key 不进入 URL、请求正文、项目副本、Run 快照或阶段指纹。

Preset 与规范化思考响应已落地；相关 schema 仍可直接升级仓库内调用方，
不保留未文档化旧字段。

### Document Adapter 与可信 Python 插件（Beta）

内置 TXT、EPUB、统一 Document Adapter 和可信 Python 插件发现已实现。仍需：

- 用更多真实 EPUB 验证 spine、命名空间、导航、CSS、图片、字体和跨节点文本；
- 明确 Adapter 版本和不透明状态升级策略；
- 建立外部 Document Adapter 契约测试；
- 维护至少一个独立发行的真实 Python Document 插件。

Python LLM Adapter 只有 provisional 边界。只有出现 JSON POST 无法表达的真实
端点后才实现首个适配器，不用模拟需求扩张协议。

### SDK 冻结门槛

每类协议独立满足以下条件后才能标记稳定：

- 至少两个真实且持续维护的实现；
- 统一契约测试与端到端测试通过；
- 配置变化、指纹、恢复、插件缺失和版本升级行为明确；
- 协议版本范围、兼容政策和废弃周期已经记录。

Document Adapter 在 TXT、EPUB 和至少一个外部插件稳定后冻结。Python LLM
Adapter 必须等待 OpenAI-compatible JSON Adapter 之外的真实特殊协议。

## 明确不规划

当前不建设远程插件、多语言进程协议、在线市场、自动安装、插件沙箱、自动
Provider 判断、静默 fallback、通用 DOM/排版树或通用工作流引擎。
