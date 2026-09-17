# 用户指南

本文介绍 Another LLM Translator 的本地 Web、macOS 桌面壳和 CLI 用法。
一般用户建议优先使用 Web 或桌面界面；CLI 适合自动化和精确控制执行范围。

本文只说明如何操作以及操作后可以观察到的结果。稳定产品语义见[最小产品规范](MINIMAL.md)；
Adapter、Preset、插件和具体格式契约见[Adapter 契约](ADAPTERS.md)。

## 1. 选择使用方式

### 本地 Web

本地 Web 在本机启动服务，通过浏览器提供项目、术语、翻译、校对、润色、设置和导出界面。

从源码启动需要 Python 3.11+ 和 Node.js/npm：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --prefix web
npm run build --prefix web
python -m app.web
```

官方 SRT Adapter 和建议级术语校验插件随应用的 `plugins/` 资源提供。用户插件可以手动
解压到 `<用户数据目录>/plugins/<插件目录>/`，重启应用后生效；插件目录必须包含契约中
规定的 `plugin.toml`、`__init__.py` 和入口模块。插件加载失败会在启动时显示具体路径、
阶段和原因，不会自动改用其他格式或发现方式。

从 Python wheel 安装时，官方插件会随应用的内置资源一起安装；用户插件仍放在用户数据根
目录，不要修改安装目录中的文件。

从本仓库源码开发时可使用 `python -m pip install -r requirements-dev.txt`，示例插件不
单独安装。

Another LLM Translator 只识别当前包名、命令、环境变量、插件入口和默认数据目录。旧版本位置中的
数据不会自动发现、迁移或删除；如需保留，请用户自行处理。

打开 `http://127.0.0.1:8765`。使用 `python -m app.web --port PORT` 可以更换端口。
前端构建完成后，日常启动只需激活虚拟环境并运行 `python -m app.web`。

### macOS 桌面壳

桌面壳使用与 Web 相同的项目存储和执行逻辑，在独立应用窗口中运行，并提供原生文件、
文件夹和导出位置选择器。当前仓库没有可直接下载的 GitHub Release，需要从源码构建；
参见[开发指南](DEVELOPMENT.md#4-macos-桌面开发)。

Windows 和 Linux 桌面版本当前尚未公开提供。其他平台可以使用本地 Web。

## 2. 配置模型连接与凭据

首次翻译前，至少需要一个可用的 LLM Preset。Preset 保存 Base URL、模型、Chunk 输入目标、限流参数、Adapter
和凭据引用，但不保存 API Key 本身。

1. 打开右上角“设置”。不需要先打开项目也可以编辑全局设置。
2. 进入全局 Preset 管理，选择一个内置示例或创建同名用户版本。
3. 选择定义请求 Endpoint 的 Adapter，填写实际的 Base URL、模型、目标 Chunk 输入 Token 和限流参数。目标值会用于该 Preset 所在阶段的 Chunk 分组；它不在项目设置中配置。RPM 和
   ITPM 按每个 Key 独立计算；“总并发”限制整个 Preset，“单 Key 并发”限制每个 Key。
   如果 Adapter 支持 SSE，可在 Preset 中开启“流式请求”；普通与流式路径均由 Adapter 定义。
4. 明确选择一种凭据来源：环境变量或系统钥匙串。两者不会互相回退。
   若希望使用系统钥匙串，请先在凭据管理中写入对应凭据。
5. 保存后可以手动检测模型列表，并查看已脱敏的请求预览和诊断信息。

全局配置中的目标语言是新项目的默认值。现有项目拥有自己的配置副本，需要在打开项目后
进入“项目设置”修改；全局配置变化不会自动改写已有项目。

### 使用环境变量

例如，Preset 引用 `LLM_API_KEY` 时，需要在启动 Web 或桌面开发壳前设置：

```bash
export LLM_API_KEY="your-api-key"
python -m app.web
```

内置示例通常使用以下环境变量：

| Preset | Adapter | 示例凭据变量 |
| --- | --- | --- |
| `default` | `openai-compatible` | `LLM_API_KEY` |
| `openai-responses` | `openai-responses` | `OPENAI_API_KEY` |
| `google-gemini` | `google-gemini` | `GEMINI_API_KEY` |
| `anthropic-claude` | `anthropic` | `ANTHROPIC_API_KEY` |

这些 Preset 只是示例，使用前必须核对端点、模型和限流设置。

### 使用系统钥匙串

在设置页的凭据管理中保存密钥（每行一个 API Key），再让 Preset 引用对应的钥匙串凭据。
项目配置、运行记录和日志只记录引用，不记录密钥正文。

不要把 API Key 写入 TOML、Preset JSON、Prompt、项目源文件或日志。

### 流式请求（Stream）

流式请求会持续接收 SSE 事件，以降低代理在等待完整响应时返回 HTTP 504 的概率。
Preset 可以显式关闭连续读取超时，但不会取消连接超时。

不同 Adapter 对流结束的判断可能不同，保存前都会按其协议确认响应完整。具体规则见
[Adapter 契约](ADAPTERS.md#sse-流式规则可选)。

> [!WARNING]
> 流式请求不能解决上游迟迟不发送首事件或代理缓冲 SSE 的问题。
> 连接中断时，已收到的半成品会被丢弃，并按既有重试次数重新请求，**可能造成重复计费**。
> 只有完整响应通过格式解析和校验后才会记录结果和展示在仪表盘详情中。

某些 Provider 会用外层 HTTP 200 传输错误事件；诊断中会
分别显示实际 HTTP 状态和 Provider 报告的上游状态，例如“HTTP 200 · 上游 HTTP 504”。
诊断页还会显示传输方式、事件数、接收字节数和首事件延迟，不会展示未校验的增量正文。

## 3. 创建和管理项目

### 创建项目

在项目选择菜单中选择“新建项目”，填写项目名、项目父目录并添加源文件。未选择文件时会
创建空项目，之后可以继续添加内容。项目使用创建时的全局目标语言默认值；创建后可在
项目设置中修改。

- Web 浏览器模式通过上传或服务端目录浏览器添加文件。
- macOS 桌面壳可使用原生文件或文件夹选择器。
- 一个项目可以包含多个 TXT、EPUB 和已安装插件支持的文件。
- 选择文件夹导入时，文件夹内按保留的相对路径自然排序，例如 `chapter2.txt` 位于
  `chapter10.txt` 之前；分批选择和单独文件的顺序保持不变。
- 运行阶段任务期间不能添加、移除或重排文件。

项目创建时会复制源文件，并生成独立的项目设置和 Prompt 副本。之后修改全局模板不会
自动改变现有项目；可以使用项目设置中的全局模板同步，或在项目 Prompt 页面载入当前
全局内容后明确保存。

项目 Prompt 页面会按当前阶段和语言显示项目副本是否与实际生效的全局 Prompt 同步。
“载入全局（未保存）”只替换编辑器草稿和预览，不写入项目文件；内容必须经过“验证并保存”
才会生效。

用户数据目录中的“提示词仓库”按阶段和语言保存命名模板。载入仓库条目同样只更新草稿，
显式保存项目 Prompt 后才会参与运行。

### 打开和删除项目

项目可以从用户数据目录的项目列表打开，也可以通过绝对路径打开外部项目。外部项目不会被移动。

项目有运行中任务时，仍可以切换项目、刷新页面并查看概览、术语、阶段结果和任务进度。上述读取不会获取项目写锁，也不会自动补齐 Prompt 或升级 SQLite。文件变更和其他项目写操作仍受同一写锁保护，运行中会明确拒绝；需要执行 Prompt/SQLite 修复时，请在项目概览中显式点击“修复项目”，并在任务结束或取消后重试。

“从项目移除文件”会让该文件不再参与后续统计、复用和导出，但既有历史结果与输出文件会
保留。“删除项目”会永久删除整个项目目录；执行前应确认没有需要保留的数据或未完成的 Run。

项目概览中的文件顺序也是后续浏览、范围选择、处理和导出的顺序。桌面端使用文件行左侧的
拖动把手重排。拖动已选文件时，全部选中文件会按当前顺序一起移动并收拢为连续组；拖动
未选文件时，只移动该文件。

窄屏或触屏设备可进入“排序”模式，选择一个文件后使用“置顶 / 上移 / 下移 / 置底”。
每次移动都会立即保存，失败时恢复移动前顺序。重排不会改变文件身份，也不会删除已有译文、
历史结果或该文件的导入设置。

具有运行格式设置的文件会显示“设置”。可以逐 File 修改，或使用概览顶部的“统一设置”按 Document Adapter 对项目内该 Adapter 的全部当前 File 部分覆盖；批量操作不会依据当前勾选的文件筛选。运行格式设置只影响之后新建的 Run，不会改写原文、定位状态或既有结果；运行确认框会只读显示统一值或“存在多种值”，可返回概览调整。

> [!WARNING]
> 请勿手工修改项目中的 `project.sqlite` 或 `input/` 内容。源文不支持增量更新；
> 需要修改时，通常应重新创建项目或重新导入文件。

## 4. 完整翻译工作流

推荐按“术语 → 翻译 → 校对 → 应用校对 → 润色 → 应用润色 → 导出”的顺序操作。
术语、校对和润色可以根据项目需要跳过。

### 4.1 术语

“术语”阶段扫描源文并生成候选术语。扫描完成后可以：

- 搜索、编辑和新增术语。
- 设置首选译名、类别、说明和别名。
- 对冲突候选进行人工裁决或建立术语组。
- 移除不应使用的术语；重新扫描不会自动恢复已移除条目。
- 手工将 alias 物化为条目时，如果命中同名的已移除条目，系统会恢复该条目及其译名、
  类别、说明和别名，再将其加入当前术语组。
- 导入或导出 JSON/CSV 术语表。
- 在扫描未完成但已有候选时，明确发布当前结果。

术语冲突不会由程序静默裁决。已发布术语会在翻译时按项目配置匹配并提供给模型。

EPUB 青空 Ruby 中的 base 和 reading 会分别参与术语匹配。直接相邻 Ruby 的 reading
会连续组合，普通正文会切断组合，base 与 reading 不会拼接。例如，
`｜漢《かん》｜字《じ》` 可以命中“漢字”和“かんじ”；同一术语重复命中只提供一次。

#### DEV: 术语自动决策

已发布术语库可以显式生成两阶段审查草案。开始前，界面会显示 Preset、可处理和受保护的
术语数量，以及请求与 Token 估算。运行期间可以查看进度。

关联术语可能需要依次决策，无法始终用满并发。新增冲突也会增加请求，因此实际用量可能
明显高于估算。关联内容需要在同一次请求中处理，极大的术语组可能超过模型输入限制。

草案支持搜索、筛选和逐条拒绝建议；组合关系建议必须整体接受或拒绝。应用、丢弃、替换
旧草案和撤销都需要再次确认。

> [!IMPORTANT]
> 自动决策不会直接修改术语库，只有人工批准的建议才会应用。

已有人工 override 的术语只作为一致性参考。应用后的移除仍是可恢复的 disabled；后续任何
术语编辑或扫描发布都会使旧撤销点失效。

#### DEV: 内容概括

在术语页打开“更多”→“内容概括 · 实验”。页面按文档提供的内容边界显示，不会把 TXT、
EPUB 或插件格式自动猜成章节。

左侧勾选“参与概括”的范围后，点击“扫描术语并补概括”。该选择会保存在当前项目中，
但只影响概括；术语扫描仍覆盖整个项目，标准术语按钮、CLI 和 `run-all` 也不会因此自动
生成概括。若当前设置无法保证概括边界，启动会明确失败并要求先调整设置。

没有搜索词时可以全选或取消全选；搜索后，相同操作只影响当前筛选结果。筛选不会清除
隐藏的选择或删除已有结果。片段生成和完整聚合使用当前参与范围，Markdown 导出则有
独立选择，因此可以导出另一组已有完整概括。

片段结果显示在“片段概括”页签。一个片段已经覆盖整个内容边界时，系统直接发布完整概括，
不额外请求模型；多个片段会按原文顺序聚合，过长时分层压缩且不截断输入。点击“查看来源”
可以检查概括引用的原文。

取消、网络或格式错误不会删除已经成功的结果，再次启动只补缺少或失败的范围。源文、
Prompt、模型或依赖结果发生变化时，页面会显示“过期”或“无法验证”等原因，不会静默复用。
旧完整概括在仍被保留时可以查看和导出，但建议重新生成。每个内容边界只保留最近三个完整
版本；只有成功发布新版本后才清理更旧且不再被保留结果引用的数据。系统无法安全判断时会
保留数据并显示 warning。

“导出 Markdown”把所选完整概括及其原文引用写入项目输出目录。没有可用完整概括或输出
路径无效时，导出会明确失败。

概括页返回术语库后才离开子页。切换到其他阶段再回来，会保留当前项目的概括子页、筛选、
边界、页签、滚动位置和来源面板状态。

### 4.2 翻译

进入“翻译”，选择运行范围并启动任务。已完成 Segment 默认复用；失败和未完成内容可以继续处理。

如需让翻译参考内容概括，可在项目设置中开启“使用前文概括”。系统只使用已完成、非空且
仍与当前源文匹配的结果：内容边界开头优先参考前一边界的完整概括，边界内部优先参考当前
边界最近的片段概括。完整概括和片段概括不会互相替代。当前分组设置不允许安全使用概括时，
系统不会注入，并会在设置或运行提示中说明。

启动前，运行对话框会显示当前阶段实际生效的 Preset ID 和模型。如果某个阶段配置了专用
Preset 覆盖值，请在确认运行前核对这些信息。

如果 Prompt、Preset、Adapter 或其他影响结果的设置已经变化，运行对话框会提示已有结果的
设置指纹不同。此时必须明确选择：

- 复用已有完成结果，只处理待处理或失败内容；或
- 重做所选范围内已有结果。

程序不会替用户自动决定，也不会因为设置变化静默清空历史结果。

翻译页面支持逐个查看和编辑 Segment。人工保存的译文会成为当前可用的翻译结果。

设置中的“翻译校验”可以选择已安装的校验器。`preferred_term_usage` 是可选的建议级术语校验：
匹配到带推荐译名的术语但候选未采用时，只发起一次修复；修复后仍未采用时，接受译文并显示
warning。该校验器默认关闭，不会强制替换过于通用的匹配。

### 4.3 校对与应用

“校对”阶段以当前翻译为基础生成建议。结果分为：

- `accepted`：建议保留当前文本。
- `suggested`：提供建议文本和原因。

生成建议不会覆盖翻译。逐条审阅后可以应用选中建议，也可以批量应用。应用操作会保存独立
结果，原翻译和校对历史仍然保留。

### 4.4 润色与应用

“润色”与校对采用相同的建议和应用机制。润色使用运行时选定的当前基准。如果希望它基于
已应用的校对结果，应先完成校对应用，再启动润色。

“运行完整流程”会依次生成术语、翻译、校对和润色结果，但不会自动应用校对或润色建议。
因此，完整流程中的润色不会隐式采用尚未应用的校对建议。

### 4.5 任务取消与恢复

阶段进度以 Segment 为单位持久化，而不是以一次临时 LLM 请求为单位。任务取消、网络失败或
应用重启后，已成功的结果仍然保留，未成功内容可以继续执行。

同一项目的写任务互斥；已有运行中的任务时，第二个写任务会明确失败。页面刷新或关闭后
重新打开时，界面会重新发现当前进程中仍在排队、运行或取消中的任务，并恢复当前项目的
进度、Token、错误和取消入口。

完成、失败或已取消的任务不会在重新打开时重建。项目结果和 Run 记录仍会持久化，但 Web
进程重启不会恢复旧的取消句柄。

Web 会记住上次选中的项目 ID。外部项目先按已保存路径重新登记并读取元数据，再关联活动任务。顶部保留
当前项目的详细任务状态条，并提供紧凑的“任务 N”全局面板，列出当前进程所有排队、运行或
取消中的任务，显示项目、阶段、状态、进度与 Token 可用性；可以打开对应项目并逐项取消任务。
如果任务对应的项目尚未在当前项目缓存中，面板会先刷新项目列表再切换到该项目；项目仍不可用时显示
明确错误。应用不提供批量取消、持久化队列或跨进程调度。

“诊断”中的“历史 Run”用于只读查看已持久化的阶段执行记录。可以按项目、阶段和状态筛选最近的
Run，打开后查看执行摘要、执行上下文与快照，以及请求级诊断；请求体、响应体和错误体只会在展开
具体请求时按需读取，并显示安全过滤或数据不可用状态。JSON 内容可以切换为“可读字段”或“原始 JSON”显示；
可读字段按需展开嵌套节点并保留字符串换行，原始 JSON 仍只包含安全过滤后的内容。

## 5. 导出

在“导出”中选择：

- 结果阶段：翻译、已应用校对或已应用润色。
- 输出格式：保留各文件原格式，或统一输出 TXT。
- 文件范围：全部文件或指定 File。
- 单语或双语对照。

TXT、EPUB 和 SRT 会按各自 Document Adapter 重建。EPUB 导出会翻译已纳入 Segment 的目录文本，
并保留导航链接、元数据、图片、CSS、字体和其他未翻译资源；模型输出不会作为任意 HTML 直接写入文档。

> [!IMPORTANT]
> 部分 Adapter（如 EPUB）要求项目设置目标语言标签，否则无法导出。

导出不会把多个 File 合并为一个文件，也不支持单独导出某个 Segment。Web 可以逐个下载
输出，也可以下载 zip；桌面壳还可以选择本机保存位置。

## 6. 支持的输入与限制

### TXT

- 支持 `.txt` 和 `.text`。
- 支持文件、目录和递归目录导入。
- 能识别 BOM 并探测常见编码，GBK/GB2312 会映射到 GB18030。
- 保留逻辑行、空行和 Segment 顺序，但不保证原始字节、换行符、BOM 或输入编码完全往返。

### EPUB

- 支持 OPF 2.0/3.0、spine XHTML、EPUB 3 `properties="nav"` 导航 XHTML 和 EPUB 2/3 NCX；目录资源会作为待翻译内容。
- 非 spine 目录资源排在正文前，spine 内 nav 保持原位置且不重复；保留文档 part 边界及导航链接、元数据和其他未翻译资源。
- 既有 EPUB 项目能否直接读取由当前 Adapter 的版本契约决定；需要重新导入时会明确提示，
  不会静默转换。具体兼容范围见[Adapter 契约](ADAPTERS.md#epub-06)。
- 替换源文件时，替换对话框会载入该 File 当前的导入设置和运行格式设置，允许编辑并在确认前展示变化和受影响内容。

#### Ruby 标签转换

下文以 `base` 表示 Ruby 正文，以 `reading` 表示注音：
`<ruby>base<rt>reading</rt></ruby>`。

- 可将 Ruby 转换为 `aozora`、`short_xml`、`compact` 和 `base_only` 四种形式。
- `short_xml` 和 `compact` 只改变 LLM 看到的 Ruby；界面仍显示 `aozora`。
- `base_only` 完全移除 reading，只保留 base。

| ID | 格式 | 例 | 损坏重试 |
| -- | -- | -- | :--: |
| aozora | `｜base《reading》` | `｜漢字《かんじ》` | ❌ |
| short_xml | `<r><b>base</b><y>reading</y></r>` | `<r><b>漢字</b><y>かんじ</y></r>` | ✅ |
| compact | `⟦R:base\|Y:reading⟧` | `⟦R:漢字\|Y:かんじ⟧` | ✅ |
| base_only | `base` | `漢字` | - |

如果 Adapter 为模型生成的运行文本与 Segment 原文不同，系统无法安全把超长 Segment 拆成可
恢复的切片，运行会明确失败，不会猜测定位或截断内容。请缩短 Segment、提高模型上下文限制，
或关闭超长 Segment 拆分；原文与模型文本一致时仍可按上下文限制拆分。

不同模型处理这些格式的正确率可能不同。

#### Ruby 连续强调符号压缩

连续 Emphasis Ruby 会在界面内容和发送给 LLM 的内容中合并，避免文本理解错误或强调符号数量
不一致。导出 EPUB 时，应用会按可见字素簇恢复为逐字 Ruby 强调。

| 步骤 | 形式 |
| -- | -- |
| 原文 | `<ruby>漢<rt>·</rt></ruby><ruby>字<rt>·</rt></ruby>` |
| 转换 (Aozora) | `｜漢《·》｜字《·》` |
| 压缩 (用户/LLM) | `｜漢字《·》` |
| 导出 | `<ruby>漢<rt>·</rt></ruby><ruby>字<rt>·</rt></ruby>` |

### SRT（独立插件）

- 支持 `.srt`；每个字幕 cue 是一个 Segment，序号和时间行会在导出时保留。
- 接受唯一正整数序号和 `HH:MM:SS,mmm --> HH:MM:SS,mmm` 时间行，序号不要求连续。
- 单语导出替换 cue 正文；双语导出在同一 cue 中按“原文、换行、译文”排列。
- 插件不解析 HTML/ASS 样式标记，模型可能改变这些标记；cue 内不得出现空白分隔行。
- 不兼容缺序号、点号毫秒或时间行尾定位参数等非核心变体。

当前不支持 PDF、DOCX、Markdown 或任意格式互转，也不提供自动翻译质量评分。

## 7. 数据、安全与局域网共享

### 数据位置

项目、用户设置、Preset、Adapter、凭据索引和日志默认存放在平台用户数据目录。macOS 默认路径是：

```text
~/Library/Application Support/another-llm-translator/
```

开发或特殊部署可以用 `ANOTHER_LLM_USER_ROOT` 指向其他用户数据根目录。不要直接编辑
项目目录中的数据库或输入副本；请通过应用完成文件替换和项目操作。

### 存储中心

在“设置”中切换到“全局 → 存储”，即可查看用户数据根目录和已登记项目的占用汇总。页面打开或
刷新时会重新扫描，并按全局类别和项目大小展示结果；外部项目也会计入，应用安装资源不会计入。

选择项目后可以在弹窗中查看 DEBUG 运行、导出输出和日志。可清理项会显示可回收大小；SQLite、输入副本、项目
配置、Prompt、Run 快照和其他受保护数据只能查看。清理 DEBUG 时按 Run 选择，清理输出按文件选择，
日志按全局或项目日志组选择。

清理是永久删除。每次操作前都必须在确认框中确认；项目有运行中任务、扫描不完整或目标不可安全
访问时，相关操作会被禁用并显示原因。清理完成后页面会重新扫描，不会预先假定删除已经成功。

### 日志与敏感内容

普通日志不会保存完整 Prompt、源文、鉴权 Header 或未脱敏请求正文。Debug 模式会额外保存
完整请求、响应和执行诊断，其中可能包含 Prompt 与源文；处理敏感材料时不要
启用 Debug。

安装的可信 Python Document Adapter 与应用在同一进程运行，拥有当前用户进程权限，不提供沙箱。

### 局域网共享

Web 默认只允许本机回环访问，并限制 Host 和 Origin。局域网共享必须在设置页显式开启并选择接口。

- 开启认证后，局域网客户端通过登录页和会话 Cookie 访问；密码保存在系统钥匙串。
- 不开启认证时，同网段客户端拥有完整项目操作和 LLM 请求权限，界面会持续警告。
- 局域网共享使用 HTTP，不提供 TLS、多账号、角色、密码找回或公网访问。

> [!CAUTION]
> 不要把服务直接暴露到公网。

## 8. CLI（高级用法）

CLI 与 Web 共用项目存储、阶段执行、写锁、限速和恢复逻辑。以下示例在源码目录执行；
安装包用户也可以使用 `another-llm-translator` 替代 `python -m app.main`。

### 安装和帮助

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.main --help
```

CLI 默认输出 JSON 摘要，日志写入标准错误和项目 `logs/app.log`。使用 `--language system`、
`--language zh-CN` 或 `--language en` 选择界面语言。

### 快速开始

```bash
python -m app.main init novel.txt --name novel
python -m app.main run-all novel
python -m app.main inspect novel
python -m app.main apply novel --stage proofreading --all
python -m app.main apply novel --stage polishing --all
python -m app.main export novel --stage polished --bilingual
```

`run-all` 不会隐式应用校对或润色建议。如果希望润色基于已应用的校对结果，应分阶段运行：

```bash
python -m app.main terminology novel
python -m app.main translate novel
python -m app.main proofread novel
python -m app.main apply novel --stage proofreading --all
python -m app.main polish novel
python -m app.main apply novel --stage polishing --all
python -m app.main export novel --stage polished
```

### 项目和文件

项目默认创建在用户数据根目录的 `projects/<name>/`。也可以指定父目录，或使用项目绝对路径代替项目名：

```bash
python -m app.main init novel.txt --name novel
python -m app.main init ./books --recursive --name collection
python -m app.main init --empty --name novel
python -m app.main init --empty --name novel --parent-dir /absolute/parent
python -m app.main files-add novel chapter-1.txt chapter-2.txt
python -m app.main files-add novel appendix.epub
python -m app.main files-remove novel F0001
python -m app.main files-replace novel F0001 chapter-1-revised.txt --dry-run
python -m app.main files-replace novel F0001 chapter-1-revised.txt --yes
```

`files-replace` 保留目标 File 的身份、顺序和导出文件名，并用新文件更新内容与导入设置。
预览会报告可保留、新增、移除和无法确定的内容，以及受影响的阶段进度。系统只复用能够
确定未变化的内容；新增、修改、缺失或无法唯一确认的重复内容从零开始。`--dry-run` 只预览；
交互终端默认显示预览后询问确认，脚本需显式使用 `--yes`。

替换时 `--adapter-option ADAPTER.OPTION=VALUE` 只覆盖指定选项；未指定选项沿用
目标 File 的当前值。Web 替换对话框具有相同语义，并在确认前展示选项变化。

可以用 `files-options` 查看或修改 File 的运行格式设置：

```bash
python -m app.main files-options novel --file-id F0001
python -m app.main files-options novel --file-id F0001 \
  --adapter-option epub.ruby_mode=short_xml
python -m app.main files-options novel --document-adapter epub
python -m app.main files-options novel --document-adapter epub \
  --adapter-option epub.inline_format_mode=markers
```

`--file-id` 只作用于一个 File；`--document-adapter` 作用于项目内该 Adapter 的全部当前
File。省略 `--adapter-option` 时只查看设置；指定它时只覆盖列出的选项，其他选项保持原值。
设置变化只影响之后新建的 Run，不改写 Segment、定位状态或历史结果。

项目存在运行中的任务或未发布的术语扫描候选时，必须先结束该任务或发布/丢弃候选。
历史阶段结果和既有输出不会被自动删除，已发布术语也不会因源文件替换而自动移除。

EPUB 可以显式指定 Adapter 和运行格式设置：

```bash
python -m app.main init book.epub \
  --name book \
  --document-adapter epub \
  --adapter-option epub.ruby_mode=aozora
```

任意 Document Adapter 的选项都使用可重复的 `--adapter-option ADAPTER.OPTION=VALUE`。

### 阶段和范围

主要阶段命令包括：

- `terminology`：扫描并发布术语。
- `translate`：执行翻译。
- `proofread`：生成校对建议。
- `polish`：生成润色建议。
- `run-all`：依次运行完整建议流程，不自动 apply。
- `apply`：应用校对或润色建议。
- `inspect`：查看项目状态、结果和设置指纹。

翻译、校对和润色可以限制到 File 或 Segment：

```bash
python -m app.main translate novel --only-file F0001
python -m app.main translate novel --only-segment F0001-S000001
python -m app.main translate novel --from-file F0002
```

阶段命令的关键控制选项：

- `--dry-run`：只报告范围、请求与 Token 估算和必要决策，不写项目、不创建 Run、不调用 LLM。
- `--force`：重做所选范围内已有的 completed 结果。
- `--reuse-mixed-fingerprints`：明确复用设置指纹不同的已完成结果。
- `--resume-run`、`--decline-run`：在非交互环境明确处理同阶段未完成 Run。

以具体子命令的 `--help` 为准，例如 `python -m app.main translate --help`。

### 术语交换

```bash
python -m app.main terms-export novel glossary.json
python -m app.main terms-export novel glossary.csv
python -m app.main terms-import novel glossary.json
python -m app.main terms-export novel scanned.json --source scanned
python -m app.main terms-publish-partial novel
python -m app.main terms-decide novel --dry-run
python -m app.main terms-decide novel
python -m app.main terms-decide novel --resume-run
python -m app.main terms-decide novel --force
python -m app.main terms-decide-show novel
python -m app.main terms-decide-apply novel --all --reject TDP-EXAMPLE
python -m app.main terms-decide-rollback novel --confirm
```

术语导入会先完整校验再合并，不会删除文件中未出现的条目。人工 override 优先于自动扫描结果，冲突不会被静默裁决。`terms-decide` 不会加入 `run-all`，也不会自动应用。存在待处理草案时必须使用 `--replace-draft` 才能生成替代草案；替代生成失败时旧草案保持不变。

`terms-decide-apply` 必须提供 `--all`，可重复使用 `--reject` 排除建议。撤销只接受最近一次
仍可撤销的应用；术语库之后发生变化时会明确拒绝。自动决策遵守当前 Preset 的并发限制，
两个决策阶段仍依次执行。

取消任务后可用 `--resume-run` 继续未完成内容；无法安全继续的旧运行会明确要求重新开始。
`--force` 会结束未完成 Run 并从头重做，但不会隐式替换待审核草案。旧规则草案可以继续
查看、拒绝或丢弃；不能安全应用时会要求重新生成。

### 导出

```bash
python -m app.main export novel --stage translated
python -m app.main export novel --stage proofread --bilingual
python -m app.main export novel --stage polished --format txt
python -m app.main export novel --stage translated --file F0001
```

`translated`、`proofread` 和 `polished` 分别表示翻译、已应用校对和已应用润色结果。
`--format original` 按原 Document Adapter 重建，`--format txt` 统一导出为 TXT。

## 9. 相关文档

- [最小产品规范](MINIMAL.md)：稳定产品语义、核心不变量和阶段边界。
- [Adapter 契约](ADAPTERS.md)：Adapter、插件和 Preset 的协议字段与版本。
- [模块职责](MODULES.md)：实现架构、模块 ownership 和依赖方向。
- [开发指南](DEVELOPMENT.md)：环境、调试、测试和桌面打包。
- [产品路线图](ROADMAP.md)：尚未实现的方向、进入条件和暂缓原因。
