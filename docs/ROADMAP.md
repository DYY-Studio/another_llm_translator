# 产品演进路线图

本路线以已经通过测试的行为为起点。每个 Stage 独立设计、实现、测试和提交，
不为后续阶段提前建设抽象。`docs/MINIMAL.md` 只描述已经实现的行为；本文描述
未来方向和进入条件。

当前项目目录继续作为完整项目边界。不建设 Repository 层或双写路径；不增加自动
Provider 判断、静默 fallback、任意格式互转或未经真实需求验证的插件能力。
已发布 SQLite 项目与 Preset schema 的后续变更必须提供明确迁移或主版本边界
（Stage 22 起生效），不为开发期项目保留兼容路径。

## 当前基线

- TXT/EPUB、独立发行的 SRT Document Adapter、File/Segment/Chunk/Run、SQLite
  项目存储、CLI 和完整翻译流程已经实现。
- CLI 与本地 Web 共享阶段、Run、限速、恢复和项目持久化代码；项目视图与人工
  编辑逻辑均由 Web 内部职责提供。
- 本地 Web 已覆盖项目、术语、结果审校、阶段决策、apply、export 和诊断，并
  保持回环访问与显式 LAN 共享的安全边界。
- 发布品牌已统一为 Another LLM Translator；旧开发名称的公共入口删除，默认用户
  数据目录、钥匙串和浏览器存储提供一次性迁移。
- 产品路线 Stage 1 至 Stage 23.4 已完成；下一阶段是 Stage 23 Windows Tauri
  公开 Beta 与 Stage 24 多 API Key。
- 已完成 Stage 的行为验收见 `docs/MINIMAL.md`（§7 核心验收矩阵）；逐 Stage
  实现细节可在 git 历史检索，本文不再重复记录已完成内容。

## 已完成 Stage 速览

| Stage | 主题 |
| --- | --- |
| 1 | 项目文件生命周期：多文件追加/移除、运行中拒绝增删、删除保护 |
| 2 | 结构化项目设置：Web 分组表单、严格校验原子写回 |
| 3 | 全局设置、Prompt 与实时 LLM Preset（`extra_body` 规则） |
| 4 | 规范化 LLM 思考响应（`reasoning_content`） |
| 5 | File 级 Document Adapter 与多格式项目 |
| 6 | 按文件筛选导出 |
| 7 | 四阶段独立 LLM Preset |
| 8 | 外部项目位置与最近路径记录 |
| 9 | 项目创建与输入队列（服务端目录浏览） |
| 10 | Adapter 模型发现与 usage 精确统计 |
| 11 | EPUB Ruby 与 Adapter 导入选项 |
| 12 | 全局 Run 状态条 |
| 13 | 诊断仪表盘与全局日志 |
| 14 | 可选 Aozora Ruby 还原 |
| 15 | EPUB 普通内联格式与 Adapter 输出契约 |
| 16 | 移除独立开发者 Editor |
| 17 | 按需 Chunk 规划与请求内短 ID |
| 18 | SQLite 标准项目存储（schema v3，v1/v2 原地迁移，旧 JSONL 不迁移） |
| 19 | 20,000+ Segment Web 性能（窗口化查询与虚拟化） |
| 20 | 中文与英文国际化（界面、CLI、Prompt 分语言） |
| 20.1 | 跨边界 Chunk 与 Run 诊断指标 |
| 21 | 桌面共享运行时、凭据与局域网（回环守卫 + 显式 LAN 认证） |
| 22 | macOS Tauri 公开 Beta（schema 冻结边界自此生效） |
| 23.1 | 导出文件浏览与局域网下载（限制在项目 `output/` 内） |
| 23.2 | 导出页标签化与列表可用性 |
| 23.3 | 导出页标签栏稳定与双栏工作台 |
| 23.4 | 四个内置 LLM Adapter 的 SSE 流式请求与诊断聚合 |
| — | 发行包资源完整性：wheel/sdist 内置资源、用户根同名覆盖优先 |

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

当前 schema 2 已覆盖类型化占位符、任意嵌套 body、自定义认证 Header、响应
JSON Pointer 负索引扩展、`messages_format` 消息形状转换与 `${system}` 与
`${model}` 占位符。HTTP、限速、重试、取消、Run 收尾和调试记录始终由宿主
管理；API Key 不进入 URL、请求正文、Run 快照或阶段指纹。

Preset schema 4 的 `stream`/`stream_endpoint`、SSE 读取超时开关与四个内置
Adapter 的 SSE 规则
已落地；普通 Preset 迁移后仍为非流式。宿主聚合完整正文，流中断丢弃半成品并
沿既有尝试次数重试，不隐式 fallback；诊断只展示事件、字节和首事件延迟。Stage
22 公开 Beta 后，已发布 Preset/Adapter schema 变更必须提供明确迁移或主版本边界。

### Document Adapter 与可信 Python 插件（Beta）

内置 TXT、EPUB、统一 Document Adapter 和可信 Python 插件发现已实现。SRT 已作为
`plugins/srt/` 独立发行包示例和
`plugins/term_validation/` 独立 Translation Validator 示例均已接入，并通过
entry point、宿主集成和冻结 sidecar 装配验证。外部 Document Adapter 契约测试（`tests/test_document_adapter_contract.py`）
与 Adapter 版本/opaque_state 升级策略（严格版本匹配、重新导入升级，见
`docs/ADAPTERS.md` §2）已落地。仍需：

- 用更多真实 EPUB 验证 spine、命名空间、导航、CSS、图片、字体和跨节点文本；
- 用真实字幕样本持续验证 SRT 核心语法边界与模型标记兼容性；
- 在出现第二个真实外部 Document Adapter 后再决定 Document Adapter 的兼容范围与运行时安装策略。

翻译校验器现在通过独立的可信 Python Validator 契约扩展；共享插件协议版本为 `10`，接收源文、
译文和宿主确定的逐 Segment 术语命中，不建立通用 DOM 或自由 HTML 协议。独立的
`another-llm-translator-term-validation` 是首个真实外部 Validator 示例；官方桌面在
构建时装配它但默认关闭，基础 Python 包可选安装，外部插件仍按版本不匹配快速失败。

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
Provider 判断、静默 fallback、通用 DOM/排版树或通用工作流引擎。桌面端不另建
业务后端，不改用 Electron，不面向公网部署，也不在
公开 Beta 前建设自动更新、TLS 证书、多账号权限或远程遥测。超大列表不自制
虚拟滚动算法，SQLite 不引入 ORM，未经过真实剖析不加入 `orjson` 或 `lxml`。
