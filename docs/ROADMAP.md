# 产品路线图

本文只记录尚未实现的方向及其进入条件。已经实现的行为和验收要求见
[`MINIMAL.md`](MINIMAL.md)；历史实施过程可从 Git 记录中检索。

后续工作继续遵循以下边界：

- 项目目录仍是完整项目边界，不引入 Repository 层或双写路径。
- 不自动判断 Provider，不静默 fallback，也不提供任意格式互转。
- 新能力必须由真实需求和可测试规则驱动，不提前建设通用框架。
- 已发布的 SQLite 项目、Preset 和 Adapter schema 如需变更，必须提供明确迁移或主版本边界。

## 优先方向

### Windows Tauri 公开 Beta

- 完成 Windows sidecar、Credential Manager、安装包签名、Unicode 和长路径验收。
- 验证外部项目、卸载后保留用户数据以及安装和升级流程。
- 只有开启局域网共享时才请求必要的防火墙权限。
- 继续采用手动升级，不引入 Windows 专属业务分支。

### 单 Preset 多 API Key

该能力只能在单凭据引用和限流观测稳定后设计。实现前必须确定：

- 每个 Key 的 RPM 和 ITPM 归属；
- Key 的选择顺序、失效判断和恢复条件；
- 费用审计和 Run 快照内容。

多 API Key 不扩展为 Provider fallback，也不静默掩盖鉴权或配额错误。

### Linux Tauri Beta

- 确定目标发行版、安装包形式，以及 Wayland 和 X11 的支持范围。
- 验证 sidecar、Secret Service、项目路径、文件权限和局域网共享。
- 缺少可用 Secret Service 时，明确失败或要求使用 `environment` 凭据，不保存明文密钥。
- 不承诺未经测试的发行版或桌面环境。

## 协议成熟度

产品功能和协议稳定性分别推进。功能可用不代表 SDK 已冻结。

### 声明式 JSON LLM Adapter

schema 2 已覆盖类型化占位符、嵌套 body、自定义认证 Header、响应 JSON Pointer
负索引、消息形状转换和 SSE。HTTP、限速、重试、取消、Run 收尾和调试记录仍由宿主管理。

后续只在现有声明能力无法表达真实端点时扩展 schema。API Key 不得进入 URL、请求正文、
Run 快照或阶段指纹。

### Document Adapter 与可信 Python 插件（Beta）

继续补充以下真实样本验证：

- EPUB spine、命名空间、导航、CSS、图片、字体和跨节点文本；
- SRT 核心语法边界及模型对样式标记的处理；
- 外部 Document Adapter 的版本升级和运行时安装需求。

只有出现第二个真实且持续维护的外部 Document Adapter 后，才决定兼容范围和运行时安装策略。
Translation Validator 保持窄接口，只接收源文、候选译文和宿主确定的逐 Segment 术语命中；
不建立通用 DOM 或自由 HTML 协议。

### Python LLM Adapter（provisional）

只有出现声明式 JSON POST 无法表达的真实端点后，才设计首个 Python LLM Adapter。
在两个真实实现共同验证前，不提供动态加载器或兼容承诺。

### 稳定门槛

每类协议必须独立满足以下条件，才能标记为稳定：

- 至少两个真实且持续维护的实现；
- 统一契约测试和端到端测试通过；
- 配置变化、指纹、恢复、插件缺失和版本升级行为明确；
- 协议版本范围、兼容政策和废弃周期已经记录。

Document Adapter 需要 TXT、EPUB 和至少一个外部插件稳定后才能冻结。Python LLM Adapter
必须等待 OpenAI-compatible JSON Adapter 之外的真实特殊协议。

## 明确不规划

当前不建设远程插件、多语言进程协议、在线市场、自动安装、插件沙箱、自动 Provider 判断、
静默 fallback、通用 DOM、排版树或工作流引擎。桌面端不另建业务后端，也不改用 Electron。

应用不面向公网部署。公开 Beta 前不建设自动更新、TLS 证书、多账号权限或远程遥测。
超大列表不自制虚拟滚动算法；SQLite 不引入 ORM；未经过真实剖析，不加入 `orjson` 或
`lxml`。
