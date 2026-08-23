# 用户指南

本文介绍 Another LLM Translator 的本地 Web、macOS 桌面壳和 CLI 用法。普通用户建议优先使用 Web 或桌面界面；CLI 放在文档末尾，适合自动化和精确控制执行范围。

## 1. 选择使用方式

### 本地 Web

本地 Web 是当前最完整、最直接的使用方式。它在本机启动服务，并通过浏览器提供项目、术语、翻译、校对、润色、设置和导出界面。

从源码启动需要 Python 3.11+ 和 Node.js/npm：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --prefix web
npm run build --prefix web
python -m app.web
```

需要处理 SRT 时，另行安装独立插件：

```bash
python -m pip install another-llm-translator-srt
```

需要使用建议级术语校验时，另行安装示范插件：

```bash
python -m pip install another-llm-translator-term-validation
```

从本仓库源码开发时可使用 `python -m pip install -r requirements-dev.txt`。官方桌面构建会在构建时装配这两个官方示例插件；已发布桌面应用暂不提供运行时插件安装。

Another LLM Translator 不再提供旧开发名称对应的命令、环境变量或插件入口。首次启动时，
若新的默认数据目录不存在，程序会一次性迁移旧默认目录；新目录已存在时保留两者并以新目录为准。

打开 `http://127.0.0.1:8765`。使用 `python -m app.web --port PORT` 可以更换端口。前端构建完成后，日常启动只需激活虚拟环境并运行 `python -m app.web`。

### macOS 桌面壳

桌面壳使用与 Web 相同的项目存储和执行逻辑，但在独立应用窗口中运行，并提供原生文件、文件夹和导出位置选择器。当前仓库没有可直接下载的 GitHub Release，需要从源码构建；参见[开发指南](DEVELOPMENT.md#5-macos-桌面开发与打包)。

Windows 和 Linux 桌面版本当前尚未公开提供。其他平台可以使用本地 Web。

## 2. 配置模型连接与凭据

首次翻译前，至少需要一个可用的 LLM Preset。Preset 保存端点、模型、限流参数、Adapter 和凭据引用，但不保存 API Key 本身。

1. 打开右上角“设置”。不需要先打开项目也可以编辑全局设置。
2. 进入全局 Preset 管理，选择一个内置示例或创建同名用户版本。
3. 填写实际的 API 端点、模型和限流参数，并确认 Adapter 与端点协议匹配。
   如果 Adapter 支持 SSE，可在 Preset 中开启“流式请求”；需要独立路径时再填写
   流式 Endpoint，留空表示复用普通 Endpoint。
4. 在 `credential` 中明确选择一种凭据来源：环境变量或系统钥匙串。两者不会互相回退。
5. 保存后可以手动检测模型列表，并查看已脱敏的请求预览和诊断信息。

全局配置中的目标语言是新项目的默认值。现有项目拥有自己的配置副本，需要在打开项目后进入“项目设置”修改；全局配置变化不会自动改写已有项目。

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

在设置页的凭据管理中保存密钥，再让 Preset 通过 `keychain` 和凭据 ID 引用它。项目配置、Preset、Run 快照和日志只记录引用，不记录密钥正文。

不要把 API Key 写入 TOML、Preset JSON、Prompt、项目源文件或日志。

### 流式请求 (Stream)
流式请求会持续接收 SSE 事件，以降低代理在等待完整响应时返回 HTTP 504 的概率；可配置允许接收 SSE 时忽视传输超时。

部分 Adapter（内置 OpenAI-compatible）还显式兼容“至少收到一个合法事件后自然
EOF”的 clean EOF；其他 Adapter 仍要求其声明的终止事件。它不能解决上游迟迟不发送
首事件或代理缓冲 SSE 的情况；连接中断时已收到的半成品会被丢弃并按既有重试次数
重新请求，可能造成重复计费。只有完整响应通过格式解析和校验后才会写入 Segment。

某些 Provider 会用外层 HTTP 200 传输错误事件；诊断中会
分别显示实际 HTTP 状态和 Provider 报告的上游状态，例如“HTTP 200 · 上游 HTTP 504”。
诊断页还会显示传输方式、事件数、接收字节数和首事件延迟，不会展示未校验的增量正文。

## 3. 创建和管理项目

### 创建项目

在项目选择菜单中选择“新建项目”，填写项目名、项目父目录并添加源文件。未选择文件时会创建空项目，之后可以继续添加内容。项目使用创建时的全局目标语言默认值；创建后可在项目设置中修改。

- Web 浏览器模式通过上传或服务端目录浏览器添加文件。
- macOS 桌面壳可使用原生文件或文件夹选择器。
- 一个项目可以包含多个 TXT、EPUB 和已安装插件支持的文件。
- 文件夹内按保留的相对路径自然排序，例如 `chapter2.txt` 位于
  `chapter10.txt` 之前；分批选择和单独文件的顺序保持不变。
- 运行阶段任务期间不能添加、移除或重排文件。

项目创建时会复制源文件，并生成独立的项目设置和 Prompt 副本。之后修改全局模板不会自动改变现有项目；可以使用项目设置中的全局模板同步，或在项目 Prompt 页面载入当前全局内容后再明确保存。

项目 Prompt 页面会按当前阶段和语言显示项目副本是否与实际生效的全局 Prompt 同步。不一致时，“载入全局（未保存）”只替换编辑器草稿和预览，不写入项目文件；确认内容后仍需点击“验证并保存”。页面中的“提示词仓库”属于用户数据目录，按阶段和语言保存命名模板。载入仓库条目同样只更新草稿；只有显式保存项目 Prompt 后，内容才会参与项目运行。

### 打开和删除项目

项目可以从用户数据目录的项目列表打开，也可以通过绝对路径打开外部项目。外部项目不会被移动。

“从项目移除文件”会让该文件不再参与后续统计、复用和导出，但既有历史结果与输出文件会保留。“删除项目”会永久删除整个项目目录，应先确认没有需要保留的数据，也没有未完成的 Run。

项目概览中的文件顺序就是后续浏览、范围选择、处理和导出的顺序。桌面端使用文件行左侧的拖动把手即可重排；拖动已选文件时，全部选中文件会按当前顺序一起移动并收拢为连续组，拖动未选文件则只移动该文件。窄屏或触屏设备可进入“排序”模式，选择一个文件后使用“置顶 / 上移 / 下移 / 置底”。每次移动都会立即保存，失败时恢复移动前顺序。重排不会改变 File/Segment ID，也不会删除已有译文、历史结果或 Adapter 状态。

请勿手工修改项目中的 `project.sqlite` 或 `input/` 内容。修改源文不支持增量更新，通常应重新创建项目或重新导入文件。

## 4. 完整翻译工作流

推荐按“术语 → 翻译 → 校对 → 应用校对 → 润色 → 应用润色 → 导出”的顺序操作。术语、校对和润色可以根据项目需要跳过。

### 4.1 术语

“术语”阶段扫描源文并生成候选术语。扫描完成后可以：

- 搜索、编辑和新增术语。
- 设置首选译名、类别、说明和别名。
- 对冲突候选进行人工裁决或建立术语组。
- 移除不应使用的术语；重新扫描不会自动恢复已移除条目。若手工将某个 alias 物化为
  条目且命中同名的已移除条目，系统会恢复该条目并保留原有译名、类别、说明和别名，
  再将其加入当前术语组。
- 导入或导出 JSON/CSV 术语表。
- 在扫描未完成但已有候选时，明确发布当前结果。

术语冲突不会由程序静默裁决。已发布术语会在翻译时按项目配置匹配并提供给模型。
EPUB 青空 Ruby 中的 base 和 reading 会分别参与术语匹配；直接相邻 Ruby 的 reading
会连续组合，普通正文会切断组合，base 与 reading 不会拼接。比如
`｜漢《かん》｜字《じ》` 可以命中“漢字”和“かんじ”，同一术语重复命中只提供一次。

#### 术语自动决策 (DEV)
已发布术语库可以通过“自动决策（开发版）”显式生成两阶段审查草案。界面会先显示
Preset、可处理/受保护术语数量及请求和 Token 估算；草案中可搜索、筛选并逐条拒绝建议，
组合关系建议必须整体接受或拒绝。应用、丢弃、替换旧草案和撤销都需要再次确认。
模型不会直接写入术语库；已有人工作为 override 的术语只作为一致性参考。应用后的移除
仍是可恢复的 disabled，后续任何术语编辑或扫描发布都会让旧撤销点失效。

自动决策的 `hit_count` 表示命中术语的 Segment 数，不是一个 Segment 内的字符出现次数。
每个术语最多提供五条不同 Segment 的上下文：先覆盖不同文件的首个命中，再按源文顺序
补充其余 Segment，因此单文件项目也可以得到五条样本。

历史类别、推荐译名和关系争用会作为去重证据交给模型，它们不是按票数选胜者的统计，也
不是封闭的可选值列表；源文和全书上下文支持时，草案可以提出候选之外的新值。第一阶段
不能保留未裁决的类别或推荐译名冲突；证据不足时会进入人工待办。Description 可以保留、
清空，或基于当前说明、源文样本和可见参考整理成简洁的目标语说明，不能添加无证据事实。

第二阶段只把人工决定和“第一阶段已确定、当前无冲突”的自动状态用作 anchors；
`needs_review`、disabled 或仍有冲突的状态不会影响其他术语。审核页会展示并搜索历史候选、
alias 归属和组关系争用，也会完整显示 Description 的旧文本与新文本。未解决关系组件会
整体恢复到运行前状态后进入人工待办，应用草案前还会再次检查术语 revision 和冲突状态。

当前决策规则版本为 6。规则版本 5 的未完成 Run 不能续作，需要结束后强制新建；旧草案
仍可查看、保存拒绝项或丢弃，但不能应用，必须重新生成。

### 4.2 翻译

进入“翻译”，选择运行范围并启动任务。已完成 Segment 默认复用；失败和未完成内容可以继续处理。

启动阶段前的运行对话框会显示当前阶段实际生效的 Preset ID 和模型；如果某个阶段配置了专用
Preset 覆盖值，请在确认运行前核对这里的内容。

如果 Prompt、Preset、Adapter 或其他影响结果的设置已经变化，运行对话框会提示已有结果的设置指纹不同。此时必须明确选择：

- 复用已有完成结果，只处理待处理或失败内容；或
- 重做所选范围内已有结果。

程序不会替用户自动决定，也不会因为设置变化静默清空历史结果。

翻译页面支持逐个查看和编辑 Segment。人工保存的译文会成为当前可用的翻译结果。

设置中的“翻译校验”可以选择已安装的校验器。`preferred_term_usage` 是可选的建议级
术语校验：匹配到带推荐译名的术语但候选未采用时只发起一次修复，仍不适用则接受译文
并显示 warning；它默认关闭，不会强制替换过于通用的匹配。

### 4.3 校对与应用

“校对”阶段以当前翻译为基础生成建议。结果分为：

- `accepted`：建议保留当前文本。
- `suggested`：提供建议文本和原因。

生成建议不会覆盖翻译。逐条审阅后可以应用选中建议，也可以批量应用。应用操作会保存独立结果，原翻译和校对历史仍然保留。

### 4.4 润色与应用

“润色”与校对采用相同的建议和应用机制。润色使用运行时选定的当前基准；如果希望它基于已应用的校对结果，应先完成校对应用，再启动润色。

“运行完整流程”会依次生成术语、翻译、校对和润色结果，但不会自动应用校对或润色建议。因此，完整流程中的润色默认不会隐式采用尚未应用的校对建议。

### 4.5 任务取消与恢复

阶段进度以 Segment 为单位持久化，而不是以一次临时 LLM 请求为单位。任务取消、网络失败或应用重启后，已成功的结果仍然保留，未成功内容可以继续执行。

同一项目的写任务互斥；已经运行任务时，第二个写任务会明确失败。Web 后台任务状态仅存在于当前进程，但项目结果和 Run 记录会持久化。

## 5. 导出

在“导出”中选择：

- 结果阶段：翻译、已应用校对或已应用润色。
- 输出格式：保留各文件原格式，或统一输出 TXT。
- 文件范围：全部文件或指定 File。
- 单语或双语对照。

TXT、EPUB 和 SRT 会按各自 Document Adapter 重建。EPUB 导出会保留导航、元数据、图片、CSS、字体和其他未翻译资源；模型输出不会作为任意 HTML 直接写入文档。

导出不会把多个 File 合并为一个文件，也不支持单独导出某个 Segment。Web 可以逐个下载输出，也可以下载 zip；桌面壳还可以选择本机保存位置。

## 6. 支持的输入与限制

### TXT

- 支持 `.txt` 和 `.text`。
- 支持文件、目录和递归目录导入。
- 能识别 BOM 并探测常见编码，GBK/GB2312 会映射到 GB18030。
- 保留逻辑行、空行和 Segment 顺序，但不保证原始字节、换行符、BOM 或输入编码完全往返。

### EPUB

- 支持 OPF 2.0/3.0 和 spine XHTML。
- 保留文档 part 边界及未翻译资源。
- EPUB Adapter 0.4 可直接使用既有 0.3 File，无需重新导入.
- 导入选项在文件加入项目时确定；修改新文件的选项仍需重新导入。

#### Ruby 标签转换
<small>Ruby的正文此处表记为`base`，注音表记为`reading`。`<ruby>base<rt>reading</rt></ruby>`</small>

- 可将Ruby转换为 `aozora`、`short_xml`、`compact` 和 `base_only` 四种形式。
-  `short_xml`/`compact` 只改变 LLM 看到的 Ruby；用户在界面上看到的仍是`aozora`。
-  `base_only` 完全移除 reading，只留下 base。

| ID | 格式 | 例 | 损坏重试 |
| -- | -- | -- | :--: |
| aozora | `｜base《reading》` | `｜漢字《かんじ》` | ❌ |
| short_xml | `<r><b>base</b><y>reading</y></r>`| `<r><b>漢字</b><y>かんじ</y></r>` | ✅ |
| compact | `⟦B:base\|Y:reading⟧` | `⟦B:漢字\|Y:かんじ⟧` | ✅ | 
| base_only | `base` | `漢字` | - |

需要根据使用的模型选择其最适应的格式，以下是少量模型的测试结果，经供参考。
| 模型 | 综合成绩 | 首选 | 正确率 | 备选 | 正确率 |
| -- | :--: | -- | :--: | -- | :--: |
| Muse Spark 1.2 | ~92.1% | aozora | ~100% | short_xml | ~100% |
| MiniMax M3 | ~79.1% | short_xml | ~99% | aozora/compact | ~96% |
| GLM 5.2 | ~77.3% | short_xml | ~94% | aozora | ~92% |
| Ox Alpha | ~75.5% | compact | ~98% | aozora | ~97% |
| DeepSeek V4 Flash 0731 | ~70.7% | short_xml | ~95% | aozora | ~94% |

- **正确率**：只反应该模型的 formatter 正确性，不表示其翻译和自动决策的可靠性。
- **综合成绩**：在极端场景和极简提示词下进行的自动决策与翻译验证，由于机器校验不完全可靠，可能有部分误差。

所有模型均使用 `reasoning_effort=high, temperature=0, top_p=0.95`。

#### Ruby 连续强调符号压缩
- 避免 LLM 在处理 Emphasis Ruby 时因连续Ruby出现文本理解不当、强调符号数不一致的情况
- 连续 Emphasis Ruby 在用户可见内容和发送给LLM的内容中均合并，导出 EPUB 时会按可见字素簇恢复为
  逐字 Ruby 强调。

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
- 首版不解析 HTML/ASS 样式标记，模型可能改变这些标记；cue 内不得出现空白分隔行。
- 不兼容缺序号、点号毫秒或时间行尾定位参数等非核心变体。

当前不支持 PDF、DOCX、Markdown 或任意格式互转，也不提供自动翻译质量评分。

## 7. 数据、安全与局域网共享

### 数据位置

项目、用户设置、Preset、Adapter、凭据索引和日志默认存放在平台用户数据目录。macOS 默认路径是：

```text
~/Library/Application Support/another-llm-translator/
```

开发或特殊部署可以用 `ANOTHER_LLM_USER_ROOT` 指向其他用户数据根目录。项目数据库是项目元数据、File、Segment、术语、阶段结果和 Run 索引的权威存储。

### 日志与敏感内容

普通日志不会保存完整 Prompt、源文、鉴权 Header 或未脱敏请求正文。Debug 模式会额外保存完整请求、响应、Attempt 和 Chunk 信息，其中可能包含 Prompt 与源文；处理敏感材料时不要启用 Debug。

安装的可信 Python Document Adapter 与应用在同一进程运行，拥有当前用户进程权限，不提供沙箱。

### 局域网共享

Web 默认只允许本机回环访问，并限制 Host 和 Origin。局域网共享必须在设置页显式开启并选择接口。

- 开启认证后，局域网客户端通过登录页和会话 Cookie 访问；密码保存在系统钥匙串。
- 不开启认证时，同网段客户端拥有完整项目操作和 LLM 请求权限，界面会持续警告。
- 首版共享使用 HTTP，不提供 TLS、多账号、角色、密码找回或公网访问。

不要把服务直接暴露到公网。

## 8. CLI（高级用法）

CLI 与 Web 共用项目存储、阶段执行、写锁、限速和恢复逻辑。以下示例在源码目录执行；安装包用户也可以使用 `another-llm-translator` 替代 `python -m app.main`。

### 安装和帮助

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.main --help
```

CLI 默认输出 JSON 摘要，日志写入标准错误和项目 `logs/app.log`。使用 `--language system`、`--language zh-CN` 或 `--language en` 选择界面语言。

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
```

EPUB 可以显式指定 Adapter 和导入选项：

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

- `--dry-run`：只报告范围、Chunk 数、Token 估算和必要决策，不写项目、不创建 Run、不调用 LLM。
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

术语导入会先完整校验再合并，不会删除文件中未出现的条目。人工 override 优先于自动扫描结果，冲突不会被静默裁决。
`terms-decide` 不会加入 `run-all`，也不会自动应用。存在待处理草案时必须使用
`--replace-draft` 才能生成替代草案；替代生成失败时旧草案保持不变。`terms-decide-apply`
必须提供 `--all`，可重复使用 `--reject` 排除建议。撤销只接受最近一次可撤销应用，且
要求术语 revision 从应用后未发生变化。自动决策在每个阶段内按当前 Preset 的
`max_parallel` 并发，两个阶段之间仍严格串行。取消任务后可用 `--resume-run` 复用已经
完成的批次；剩余批次使用当前配置和 Prompt，并按检查点重新计算第二阶段的可信 anchors。
规则版本 5 的 running Run 不可续作。`--force` 会结束未完成 Run 并从头重做，但不会隐式
替换待审核草案。旧规则草案可以继续查看、拒绝或丢弃，应用时会要求重新生成。

### 导出

```bash
python -m app.main export novel --stage translated
python -m app.main export novel --stage proofread --bilingual
python -m app.main export novel --stage polished --format txt
python -m app.main export novel --stage translated --file F0001
```

`translated`、`proofread` 和 `polished` 分别表示翻译、已应用校对和已应用润色结果。`--format original` 按原 Document Adapter 重建，`--format txt` 统一导出为 TXT。

## 9. 相关文档

- [开发指南](DEVELOPMENT.md)：源码开发、桌面壳和打包。
- [MVP 规范](MINIMAL.md)：完整行为、数据结构、恢复和安全边界。
- [Adapter 契约](ADAPTERS.md)：请求模板、模型发现、usage 和文档格式契约。
- [产品路线图](ROADMAP.md)：当前阶段和后续方向。
