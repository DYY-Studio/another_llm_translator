# 产品演进路线图

本路线以已经通过测试的行为为起点。每个 Stage 独立设计、实现、测试和提交，
不为后续阶段提前建设抽象。`docs/MINIMAL.md` 只描述已经实现的行为；本文描述
未来方向和进入条件。

当前项目目录继续作为完整项目边界。SQLite 切换前不建设 Repository 层或双写
路径；不增加自动 Provider 判断、静默 fallback、任意格式互转或未经真实需求
验证的插件能力。macOS 公开 Beta 前允许直接升级项目格式和插件协议，不为开发期
项目保留迁移或兼容路径；公开 Beta 后再建立明确的数据升级边界。

## 当前基线

- TXT/EPUB、File/Segment/Chunk/Run、项目 JSONL、CLI 和完整翻译流程已经实现。
- CLI 与本地 Web 共享阶段、Run、限速、恢复和项目持久化代码；项目视图与人工
  编辑逻辑均由 Web 内部职责提供。
- 本地 Web Alpha 已覆盖项目、术语、结果审校、阶段决策、apply 和 export，并
  保持回环地址和单写任务安全边界。
- 产品路线 Stage 1 至 Stage 17 已完成；下一阶段是 SQLite 标准项目存储。

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

Adapter 定义只保存在全局目录；Preset 只引用 Adapter ID，项目实时使用全局
Adapter，不保存副本。项目、全局模板和 Run 续作均只接受
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

## Stage 9：项目创建与输入队列（已完成）

- 移除项目创建时遗留的文档格式选择；空项目不预先绑定格式。
- 保存父目录和打开项目路径默认填入服务端绝对 `projects` 路径，允许手工修改；
  不使用无法可靠返回服务端绝对路径的浏览器目录选择器。
- 新建项目使用待输入列表，允许多次添加单独文件或整个文件夹。
- 文件夹递归导入保留相对所选根目录的路径；单独文件只保留文件名。
- Document Adapter 声明一个或多个支持扩展名。扩展名按大小写不敏感匹配，
  重复归属在启动时快速失败，不猜测 Adapter。
- 文件夹批次中的不支持文件被忽略并汇总提示；没有支持文件时拒绝该批次。
- 最终导入相对路径按大小写不敏感检查冲突。待输入列表或新批次出现冲突时
  警告，并整体拒绝本次新增批次。
- 不支持 `webkitdirectory` 的浏览器明确提示无法选择文件夹，单文件导入仍可
  使用。

## Stage 10：Adapter 模型发现与 usage 统计（已完成）

- Adapter 可选声明 models 请求规格和响应映射；宿主继续负责鉴权、代理、
  超时和 HTTP 生命周期。
- Web 只在用户手动触发时检测连通性并读取模型列表，用于填写 Preset；不自动
  判断 Provider、选择模型或切换端点。
- Adapter 可选把响应中的 usage 映射为规范化计数；宿主汇总一次任务内端点
  实际返回的消耗，并在任务和 Run 摘要中展示。
- Provider 未返回 usage 时明确显示不可用，不使用本地启发式估算冒充端点
  账单或实际消耗。

模型发现和 usage 都是 Adapter 对端点响应的可选映射，与主请求的限速或重试
语义无关。实现细节见 `docs/ADAPTERS.md`。

## Stage 11：EPUB Ruby 与 Adapter 导入选项（已完成）

- 将完整 `<ruby>` 子树视为一个语义内联单元，不把基础文字、`rt` 和尾文本
  拆成互不相关的 Segment。
- 普通透明内联元素中的相邻 XHTML `text`/`tail` 槽合并为一个 Segment；未知
  结构和 `br` 形成边界，Ruby 与同一文本流的前后文及其他 Ruby 按源文顺序合并；
  只有没有相邻文本的独立 Ruby 才继续使用旧的独立 locator 形状。
- EPUB Adapter 在不透明状态中保存 Ruby 原始结构和复合定位；不引入宿主
  通用 DOM、排版树或跨格式中间表示。
- 一个 EPUB 仍对应一个 File；每个 spine XHTML 作为该 File 内部的 `part_id`。
  新建 Segment、Chunk、参考上下文以及格式/校验修复均限制在同一
  `(file_id, part_id)`，调度和列表仍按 EPUB File 进行。
- `ImportedFile.segment_part_ids` 是可选且与 Segment 对齐的导入字段；TXT 和
  普通 Adapter 使用 `document`。新项目持久化每条 Segment 的 `part_id`；旧项目
  缺少有效 part 数据时要求重新创建，不提供迁移或从 locator 补齐。
- 普通及混合 Ruby 复合 Segment 保存按源文顺序的有序槽定位；纯译文写入首个
  可用位置，清空其余普通槽并移除该 Segment 内全部 Ruby，同时保留非 Ruby
  内联标签骨架；双语导出保留完整源句和 Ruby，只在整个 Segment 末尾追加目标文本。
- 增加 Adapter 自有、类型明确且仅在导入时生效的选项。首个且唯一的真实
  用例是 EPUB `ruby_mode`：
  - `aozora`：`｜原文《Ruby》`，默认值；
  - `base_only`：仅基础文字；
  - `parenthetical`：`原文（Ruby）`。
- 选项在导入前选择并固化于 File Adapter 状态。修改选项不静默重切既有
  Segment；用户必须明确重新导入，并生成新的 File/Segment ID。
- 纯译文 EPUB 使用普通译文替换整个 Ruby 子树，移除可能失效的原始读音；
  双语 EPUB 的源文部分保留原 Ruby 结构，译文部分不生成 Ruby。
- 验收覆盖简单 Ruby、多个 base/reading 对、`rb`/`rt`/`rtc`/`rp`、Ruby
  内嵌行内元素和尾文本。无法确定语义对应关系的结构带 XHTML 位置快速失败，
  不静默展平。
- 首版不增加 Ruby Prompt 旁路上下文，不要求模型输出标记，不重建译文 Ruby，
  也不建设自由键值或嵌套的通用 Options 框架。

## Stage 12：全局 Run 状态（已完成）

- 在顶部导航下增加全局任务状态条，在概览、导出、设置和窄屏中始终可见。
- 页面内的“开始当前阶段”按钮可按页面隐藏，但全局状态和手动取消入口不可
  隐藏。
- 展示项目、阶段、状态、已处理 Segment/总 Segment、进度、当前 Run 累计
  输入 Tokens 和累计输出 Tokens；不显示 Combined Tokens。
- 已复用 Segment 计入已处理数量。
- usage 使用端点精确回报的全有或全无语义；续作累计到同一 Run。旧 Run
  无法可靠还原累计 usage 时显示不可用，不进行估算。
- 完成、失败或取消状态保留到下一任务；刷新页面不恢复已经结束的 Web 会话
  状态。

## Stage 13：诊断仪表盘与全局日志（已完成）

- 新增仪表盘，展示全局日志、当前请求并发数、当前 Run 累计输入/输出 Tokens，
  以及基于完整精确 usage 的总吞吐量。
- 普通日志不因项目切换而丢失，使用现有日志体系写入有界轮转全局日志。
- 仪表盘为当前 Run 保留最近 50 个逻辑 LLM 请求的有界内存详情；同一请求的
  HTTP 重试只追加尝试信息。详情包含规范化 messages、解析后的 Content 与
  Reasoning，并对单条内容设置字符上限。
- 诊断轮询只返回请求摘要；完整详情按请求 ID 单独读取。详情随新 Run 清空，
  应用重启后丢失，不写入普通日志、轮转文件、Run 文件或项目数据。
- 内存详情可能包含 Prompt 和源文，但不采集 Header、API Key、Adapter Wire
  Body 或 Provider 原始 REST JSON；解析和终止错误只显示安全错误类别。
- 增加请求延迟、HTTP 错误、重试、当前限流等待请求数和 usage 覆盖状态，以及
  日志级别、项目、阶段过滤、搜索和暂停自动滚动。限流等待数包含本地
  RPM/ITPM 额度、RPM 发起许可排队及 HTTP 429 的 Retry-After 或退避，不包含
  网络错误、408 和 5xx 的普通重试退避。
- usage 不完整时吞吐量显示不可用，不用本地 Token 估算冒充准确数据。
- 普通日志不得包含 API Key、鉴权 Header、未脱敏请求/响应 Payload 或源文
  调试数据。
- 不引入远程遥测、数据库日志、分布式追踪或新的通用服务层。

## Stage 14：可选 Aozora Ruby 还原（已完成）

进入条件：Stage 11 的 Ruby 文本流合并已在真实 EPUB 中稳定验证。本阶段只恢复
模型明确返回的 Ruby，不建设普通内联格式协议。

- `ruby_mode=aozora` 时由模型自由决定是否保留 Ruby；完全不返回 Ruby 是合法
  结果，不因数量减少触发重试。
- 严格解析完整的 `｜base《reading》`，并在纯译文和双语译文区域恢复为 EPUB
  Ruby；同一 Segment 可以包含多个 Ruby。
- base 和 reading 必须非空，标记不得嵌套或跨行。其他文本保持普通文本，不接受
  任意 HTML，也不按字符数猜测目标范围。
- 翻译、校对和润色 Prompt 要求 reading 翻译或转写为目标语言适用的字母或
  注音表达，但不尝试自动判断语言学正确性。
- `base_only` 和 `parenthetical` 不执行 Ruby 重建。
- 本阶段不强制保留 Ruby，不实现普通内联标记、逐标签策略或通用验证器框架。

## Stage 15：EPUB 普通内联格式与 Adapter 输出契约（已完成）

进入条件：可选 Ruby 还原行为稳定，并已有真实 EPUB 需要在译文中恢复普通
内联格式。本阶段直接扩展现有可信 Document Adapter，不建立独立校验器系统。

- EPUB 增加与 `ruby_mode` 独立的导入选项 `inline_format_mode`：
  - `plain`：默认值，不向模型暴露普通内联标记；
  - `markers`：生成受控、无 attrs 的成对标记，例如 `<em1>…</em1>`。
- Segment `source` 保存净文本，并继续包含 `ruby_mode` 产生的 Ruby 表达；Marker
  模式另持久化与 Segment 对齐的 `model_source`。术语使用净文本，翻译、校对、
  润色及其参考上下文使用模型文本。
- 原标签、attrs、定位和父子关系只保存在 Adapter 不透明状态。源文与保留标记
  语法冲突时带 XHTML 位置快速失败，不转义或猜测。
- 项目内按 Adapter 配置 `inline_format_policy`：
  - `tiered`：默认值；`a/abbr/bdi/bdo/cite/code/data/dfn/kbd/q/samp/sub/sup`
    和 `time/var` 必须保留，`b/em/i/mark/s/small/span/strong/u` 可整体省略；
  - `strict`：全部源标记必须保留。
- 实际返回的标记必须已知、唯一、成对、正确嵌套并维持原父子关系；兄弟范围
  可以随译序移动。损坏标记使用现有 `format_max_attempts` 修复预算，耗尽后该
  Segment 失败。
- 校对和润色的 `accepted` 沿用已验证基准；`suggested` 必须执行相同校验。
- `plain` 纯译文保留原普通标签和 attrs 的空骨架，不猜测译文局部格式映射；
  Ruby 仍按独立规则处理。
- Document Adapter 协议直接升级：`ImportedFile` 增加可选、与 Segment 对齐的
  模型文本；Adapter 可声明类型化 `run_options`，并可选校验结果和生成净显示
  文本。仓库内调用方同步升级，不保留旧协议分支；当前插件协议为版本 5。
- 宿主继续负责请求、修复、失败记录和 Run 收尾，Adapter 只解释自身文本契约。
  Run 保存 Adapter ID、版本和解析后的运行选项，并纳入阶段指纹。
- Segment 列表显示净文本和格式数量；详情编辑区显示原始标记和净文本预览，
  手工保存前执行相同校验。不实现富文本编辑器或逐标签配置。

## Stage 16：移除独立开发者 Editor（已完成）

- 删除 `app/editor.html`、独立 Editor HTTP 服务、命令入口、README 使用说明和
  专属测试，不保留第二套界面。
- Web 仍使用的项目视图和人工编辑逻辑改名并收敛为 Web 内部职责，不复制业务
  写入规则。
- 手工修改记录来源统一为 Web，不保留 `project_editor` 兼容值。
- CLI 与 Web 继续共享项目存储、阶段结果和写锁语义。

## Stage 17：按需 Chunk 规划与请求内短 ID（已完成）

- 每个实际 LLM 请求把待返回 Segment 临时编号为字符串 `"1"、"2"…`，响应后
  映射回持久 Segment ID；不修改项目内 File/Segment ID。
- reference context 删除无输出用途的 ID，术语扫描不发送 Segment ID。格式修复、
  验证修复、上下文拆分和超长 Segment 子请求分别建立自己的局部映射。
- 内存诊断显示短 ID 到持久 ID 的映射；项目数据、结果、日志和 debug manifest
  继续使用持久 ID。
- 普通 Run 创建后才按需要规划下一批 Chunk。调度只保留与 `max_parallel` 同阶的
  有界缓冲，Chunk ID 在进入调度时生成，debug manifest 随生成追加；取消后不再
  继续规划。
- `ordered_by_file` 继续保证单 File 顺序，`parallel` 继续受最大并发限制；所有
  请求仍不得跨 `(file_id, part_id)`。
- dry-run 明确耗尽规划器，以返回完整 Chunk 数和 Token 估算。
- 不根据模型输出动态调整 Chunk，不持久化 Chunk 业务状态，也不增加新的通用
  调度层。

## Stage 18：SQLite 标准项目存储

进入条件：模型文本、Adapter 运行选项和结果校验语义已经稳定。该阶段发生在首个
公开 Beta 前，因此直接切换项目格式。

- 新项目只使用 `project.sqlite`；旧 JSONL 项目要求重建，不提供迁移、双写或旧
  格式读取。
- SQLite 保存项目元数据、File、Segment、`model_source`、Document Adapter
  不透明状态、术语、阶段结果、Run 索引和活动任务状态。
- 原始输入、配置、Prompt、Run 配置/Adapter/Preset 快照、调试 Payload 和导出
  文件继续作为项目外围文件。
- 使用标准库 `sqlite3`、外键、事务、唯一约束、必要索引和 WAL；不引入 ORM。
- 按领域提供直接 SQL 操作，不提前建设通用 Repository 层。
- File、Segment、结果和 Adapter 状态在同一事务内保持一致；一个 EPUB 仍是
  一个 File。
- 数据库包含明确 schema version，未知版本快速失败。切换完成后删除 JSONL
  运行路径和相关兼容代码。

## Stage 19：20,000+ Segment Web 性能

进入条件：SQLite 查询和事务边界稳定。当前全量 Overview 响应和完整 DOM 列表
不作为超大项目支持方案继续扩展。

- 项目概要 API 不再返回全部 Segment；增加稳定排序的 `offset/limit` 窗口查询、
  总数和服务端文件、状态、搜索过滤。
- 提供当前过滤结果的紧凑有序 ID 索引，使 Ctrl/Cmd/Shift 选择无需加载全部行
  正文。Segment 详情按需读取，保存后只刷新受影响窗口和汇总。
- 前端采用一个成熟、支持动态行高的轻量 virtualizer，只渲染可见窗口和少量
  overscan；不手写虚拟滚动算法，不新增状态库。
- 20,000 Segment 验收必须确认：初始页面只请求有限窗口、DOM 行数保持有界，
  搜索、过滤、深度滚动、跳转、经典多选和批量操作正确，三阶段和移动布局无
  横向溢出，Run 进度不依赖已加载页面。
- `orjson` 只有在窗口化完成后仍由剖析确认序列化为主要瓶颈时才允许引入。
  `lxml` 只有真实 EPUB 正确性无法由标准库满足，或剖析确认 XML 处理为主要
  瓶颈时才允许引入。
- 二进制依赖还必须通过后续三平台 sidecar 打包验证；单独微基准更快不构成
  引入理由。

## Stage 20：中文与英文国际化

- 在 macOS 公开 Beta 前完成中文、英文两种真实界面。
- Web 和桌面语言按浏览器配置保存；CLI 提供明确语言参数并支持系统默认。
- 前端抽取消息目录、日期数字格式和可访问性文本。
- Web API 为前端可见错误提供稳定错误代码与参数，同时保留安全 fallback 文本。
- 项目内容、Prompt、目标语言和模型输出不随 UI 语言改变。
- 不实现远程语言包、自动翻译文案或插件本地化市场。

## Stage 21：桌面共享运行时、凭据与局域网

- 使用 Tauri 和打包后的 Python/FastAPI sidecar，复用现有 React、API、执行和
  存储代码，不建设第二套桌面后端。
- 区分只读应用资源与平台用户数据目录；全局配置、Preset、自定义 Adapter、
  日志和默认 `projects` 均写入用户数据目录。
- Preset schema v2 使用显式单凭据引用：`environment` 读取指定环境变量，
  `keychain` 读取系统钥匙串；两者二选一，不隐式 fallback。
- 钥匙串界面支持创建、更新、删除和测试凭据；API 永不回传密钥，Run 和日志
  只保存引用 ID。
- Tauri 提供最小原生文件、文件夹和项目路径选择桥接；普通浏览器和 LAN 客户端
  继续使用上传和服务端路径行为。
- 默认只监听回环地址。用户可以显式开启指定局域网接口，并保存一组长期用户名
  和密码；密码进入系统钥匙串。有认证时使用登录页和 HttpOnly 会话 Cookie，
  停止共享或服务重启后已有会话失效，长期账密保留。
- 用户也可在可信局域网中留空认证，但必须明确警告同网段设备拥有完整项目和
  LLM 操作权限。首版使用 HTTP，不实现 TLS 证书、多账号、角色、密码找回、
  公网部署或远程管理。
- 保持单写任务和项目写锁语义。

## Stage 22：macOS Tauri 公开 Beta

- 构建并验证 macOS Tauri 应用、Python sidecar、系统钥匙串、用户数据目录和
  原生选择器。
- 提供签名、公证的安装包及中英文首次启动引导。
- 覆盖全新安装、手动升级、项目移动、外部项目、真实钥匙串、LAN 移动访问、
  取消任务和异常退出恢复。
- 使用手动下载安装升级；不实现自动更新、远程遥测或崩溃上传。
- 从本阶段起，已发布 SQLite 项目和 Preset schema 的后续变更必须提供明确迁移
  或主版本边界，不再静默要求重建。

## Stage 23：Windows Tauri 公开 Beta

- 独立完成 Windows sidecar、Credential Manager、安装包签名、Unicode/长路径、
  外部项目和卸载保留用户数据验收。
- 只有开启 LAN 时才请求必要的防火墙权限。
- 继续采用手动升级，不引入 Windows 专属业务分支。

## Stage 24：单 Preset 多 API Key

- 在单凭据引用与限流观测稳定后，单独设计多个 credential reference。
- 实现前必须锁定每 Key 的 RPM/ITPM 归属、选择顺序、失效、恢复、费用审计和
  Run 快照。
- 不扩展成 Provider fallback，不静默掩盖鉴权或配额错误。

## Stage 25：Linux Tauri Beta

- 最后评估并实现 Linux sidecar、Secret Service、Wayland/X11、目标发行版和
  安装包形式。
- 缺少可用 Secret Service 时明确失败或要求选择 `environment` 凭据，不自行
  保存明文密钥。
- 验收项目路径、文件权限、LAN、防火墙说明和中英文界面。
- 不承诺未经测试的发行版或桌面环境。

## 协议成熟度

产品 Stage 与协议稳定性分别推进，不能因为某项进入产品路线就提前冻结 SDK。

### 声明式 JSON LLM Adapter（已实现）

当前 schema 已覆盖类型化占位符、任意嵌套 body、自定义认证 Header、响应
JSON Pointer 负索引扩展、`messages_format` 消息形状转换与 `${system}`
占位符。内置 Anthropic、Gemini 原生与 OpenAI Responses 定义，Preset
`endpoint` 支持 `${model}` 占位符。项目实时引用全局 Adapter，不保存项目
副本。HTTP、限速、重试、取消、Run 收尾和
调试记录始终由宿主管理。API Key 不进入 URL、请求正文、Run
快照或阶段指纹。

Preset 与规范化思考响应已落地。Stage 22 前相关 schema 仍可直接升级仓库内
调用方，不保留未文档化旧字段；Stage 22 公开 Beta 后，已发布 Preset schema
变更必须提供明确迁移或主版本边界。

### Document Adapter 与可信 Python 插件（Beta）

内置 TXT、EPUB、统一 Document Adapter 和可信 Python 插件发现已实现。仍需：

- 用更多真实 EPUB 验证 spine、命名空间、导航、CSS、图片、字体和跨节点文本；
- 明确 Adapter 版本和不透明状态升级策略；
- 建立外部 Document Adapter 契约测试；
- 维护至少一个独立发行的真实 Python Document 插件。

Stage 15 只在 Document Adapter 边界增加可选结果契约，不建立独立校验器插件、
通用 DOM 或自由 HTML 协议。公开 Beta 前协议版本可以直接升级并同步仓库调用方；
外部插件仍按版本不匹配快速失败。

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
Provider 判断、静默 fallback、通用 DOM/排版树、通用校验器注册中心或通用
工作流引擎。桌面端不另建业务后端，不改用 Electron，不面向公网部署，也不在
公开 Beta 前建设自动更新、TLS 证书、多账号权限或远程遥测。超大列表不自制
虚拟滚动算法，SQLite 不引入 ORM，未经过真实剖析不加入 `orjson` 或 `lxml`。
