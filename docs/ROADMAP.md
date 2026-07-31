# 产品与扩展路线

本路线以已经通过测试的行为为起点。阶段编号表示契约成熟度，不表示必须同时
冻结所有类别；LLM、Document 和 Python 插件各自达到门槛后独立稳定。

## Phase 0：MVP 基线（已完成）

TXT、File/Segment/Chunk/Run、项目 JSONL、CLI、术语、翻译、审校、apply 和
导出是稳定基线。`docs/MINIMAL.md` 是当前已实现行为的规范。

进入下一阶段的条件：

- Segment 是唯一进度单位，Chunk 不成为业务状态。
- Run 可恢复，阶段结果和设置指纹可审计。
- 完整自动化测试不访问真实模型。

## Phase 1：共享内核与声明式 LLM Adapter（已完成）

CLI、本地 Web 和开发编辑器共享项目、阶段、Run、限速和 HTTP 执行代码。
LLM 协议转换由命名 JSON Adapter 完成，内置 OpenAI-compatible 定义不再写死
在传输层。

验收门槛：

- 类型化占位符、任意嵌套 JSON 字段、自定义认证 Header 和 JSON Pointer
  均有边界测试。
- HTTP Client、限速、重试、取消、Run 收尾和调试记录始终由宿主管理。
- API Key 不能进入 URL、请求正文、项目副本、Run 快照或阶段指纹。
- Adapter 定义复制到项目并随 Run 快照；定义 Hash 进入阶段指纹。

JSON Adapter schema 已可用于项目配置，但在 1.0 前仍允许直接升级调用方，
不保留未文档化的旧字段。

## Phase 2：本地 Web Alpha（已完成）

FastAPI 与 React/Vite/TypeScript 提供本机工作台，覆盖项目创建、配置与 Prompt、
Adapter 编辑预览、阶段任务、取消、结果审校、apply 和 export。项目目录仍是
唯一数据真值。

进入 Beta 的条件：

- Web 与 CLI 对同一阶段产生相同 Run 和结果记录。
- 同项目只允许一个写任务；进程内任务和跨进程写入都明确拒绝冲突。
- 服务只绑定回环地址，并拒绝非本机 Host/Origin。
- 前端生产构建、窄屏布局、核心交互和浏览器控制台通过检查。

Alpha 不承诺远程部署、多用户、账号系统、数据库或分布式任务。

## Phase 3：Document Adapter 与可信 Python 插件 Beta（进行中）

统一 Document Adapter、内置 TXT 和首个 EPUB Adapter 已实现。EPUB 保存原包
和不透明定位状态，可导出纯译文或双语 EPUB，并保留未修改资源。可信 Python
插件通过 `minimal_llm_translator.plugins` entry-point 组发现。
项目现已支持按既有内置 Adapter 创建空项目及增删同格式文件：TXT 可多文件，
EPUB 保持单文件。多格式项目仍需等待 per-file Adapter 状态设计，不由当前项目
级不透明状态推测实现。

仍需通过更多真实文档完成 Beta：

- 用不同制作工具生成的 EPUB 验证 spine、命名空间、导航、CSS、图片、字体和
  跨节点文本。
- 明确 EPUB Adapter 版本升级和不透明状态升级策略。
- 为外部 Document Adapter 提供可复用的契约测试套件。
- 至少维护一个独立发行的真实 Python Document 插件。

Python LLM Adapter 目前只有边界设计，没有加载或实现。只有遇到 JSON POST
无法表达的真实端点（例如特殊签名、非 JSON 正文）后，才实现第一个适配器。
插件是用户主动安装并与宿主同进程执行的可信代码；不提供沙箱承诺。

## Phase 4：分类冻结 SDK（未来）

每类协议独立满足以下条件后才能标记稳定：

- 至少两个真实且持续维护的实现。
- 统一契约测试与端到端测试均通过。
- 配置变化、阶段指纹、恢复、插件缺失和版本升级已有明确行为。
- 协议版本范围、兼容政策和废弃周期已记录。

Document Adapter 在 TXT、EPUB 和至少一个外部插件稳定后冻结。Python LLM
Adapter 必须等待 OpenAI-compatible JSON Adapter 之外的真实特殊协议。

## 明确不规划

当前不建设远程插件、多语言进程协议、在线市场、自动安装、插件沙箱、自动
Provider 判断、静默 fallback、通用 DOM/排版树或通用工作流引擎。真实需求
出现前，不为这些方向增加配置和抽象。
