# 基于 LLM 的工程化翻译器

## 最小可行性验证实现文档

版本：MVP 0.3

实现语言：Python 3.11+

运行方式：单机、本地 CLI/Web、异步并发

存储方式：项目文件夹、SQLite、JSON、TOML、TXT、EPUB，以及可选的外部 SRT 插件

LLM 接口：声明式 JSON POST Adapter，支持 Preset 显式启用的 SSE 流式传输

---

# 1. 目标与边界

## 1.1 验证目标

本项目只验证下面这条工程化翻译链路是否可行：

```text
TXT/EPUB 导入（安装 SRT 插件时也支持 SRT）
→ Segment 化
→ 术语提取
→ 翻译
→ 校对建议
→ 可选应用校对
→ 润色建议
→ 可选应用润色
→ 原文档格式导出
```

MVP 需要回答：

1. TXT、EPUB 与已安装外部 Adapter 支持的格式能否稳定导入、处理并按原格式导出。
2. 文档的可翻译单元能否稳定映射为 Segment，TXT 空行是否保持可见结构。
3. 长文本能否在模型 Token 限制内动态分块。
4. 术语能否从项目文本提取、合并并注入相关翻译请求。
5. 翻译、校对和润色结果能否准确对应 Segment。
6. 上文能否帮助模型判断人物、指代、语气和语义。
7. 并发、RPM、ITPM 和有限重试是否有效。
8. 中断后能否只继续未完成 Segment。
9. 修改 Chunk、限流或调度参数后能否继续复用已完成结果。
10. 单语和双语文档是否可读，并保持格式对应的结构与资源。

术语召回率约 85%、核心术语一致率约 90% 只是在固定样本、模型、Prompt 和配置下的观测参考，不作为随机自动化硬门槛。

## 1.2 核心不变量

### File 与文档 part 是内容边界

File 仍是存储、选择和导出的边界。默认情况下，任何 LLM 请求和 Chunk 都不得跨 `(file_id, part_id)`；`reference_context` 始终不得跨该边界。

项目配置可以按阶段启用 `chunking.cross_boundary_batching`，让满足源文顺序规则的 Chunk 和对应 LLM 请求跨 File 或 part；这不改变 File 的存储、选择、导出和调度身份。术语库可以在项目级汇总。

TXT 和普通 Adapter 使用 `part_id = "document"`；EPUB 使用每个 spine XHTML 的归档路径作为 part。调度仍按 File 进行，不把 EPUB 拆成多个 File。

### Segment 是进度单位

Segment 是 Document Adapter 返回的有序可翻译单元：TXT 中对应逻辑行，EPUB 中对应 XHTML 文本流。

每个 Segment 记录非空的 `part_id`；普通透明内联元素中的相邻文本槽合并为一个语义单元；Ruby 是同一文本流中的内联语义成员，与前后普通文本共同组成一个语义单元；只有没有相邻文本的独立 Ruby 才保持旧的独立定位形状。

术语扫描、翻译、校对和润色的完成结果、失败记录和恢复判断全部绑定 `segment_id`。

外部 SRT Adapter 将每个字幕 cue 映射为一个 Segment，整个 SRT File 使用
`part_id = "document"`；序号和时间行由 Adapter 状态保存，不成为 Segment 正文。

非空 Segment 的连续前导 Unicode 空白以源文为准。模型仍接收完整文本，但翻译、
校对建议、润色建议、apply 和导出在本地移除模型生成的前导空白，并恢复源
Segment 的精确前缀；正文内部和尾随空白不做本地保护。该行为不依赖 Prompt。

空 Segment：

- 源文为空字符串或只包含 Unicode 空白字符，例如普通空格、Tab 或全角空格。
- 保留在源数据中。
- 不提交 LLM。
- 不要求写阶段结果。
- 导出时统一恢复为普通空行，不保留原始空白字符。

### Chunk 是临时请求包装

Chunk 由当前 Run 动态生成，只能包含：

- 默认只包含同一个 `file_id` 和 `part_id`；阶段启用
  `cross_boundary_batching` 时，允许按下述规则跨边界。
- 按 `line_index` 保持顺序的非空 Segment。
- 当前命令选定范围内仍需处理的 Segment。

两个待处理非空 Segment 之间只有一个或多个空 Segment 时可以进入同一 Chunk；空 Segment 本身仍不提交 LLM。

启用跨边界合并后，不同 File 之间的源文顺序边界可直接合并；同一 File 跨 part 时，两个 Segment 的 `line_index` 中间区间必须全部是空 Segment。已完成、筛选范围外或其他不在待处理集合中的非空 Segment 会结束当前 Chunk。

部分响应、格式修正和翻译校验修复也必须使用相同规则重新分组。

> [!IMPORTANT]
> Chunk 没有长期业务身份，不能用于判断进度或恢复。只有启用调试模式时才持久化
> Chunk Manifest。

### Run 是一次执行记录

除 `--dry-run` 外，术语、翻译、校对、润色和 apply 通常创建 Run。四个独立
LLM 阶段命令也可以续用同阶段最近的未完成 Run；`run-all` 不参与续作。

Run 保存：

- 阶段和选定范围。
- 当前 `stage_fingerprint`。
- 项目配置快照。
- LLM 阶段实际使用的完整 Prompt。
- 开始、结束和结果摘要。

apply 不调用 LLM，因此只保存配置和输入结果引用，不保存虚构 Prompt。

### 设置变化由用户决断

设置变化不能自动清空、隐藏或标记旧进度失效；是否复用必须由用户决定。

- 已有 completed 继续作为可用结果。
- 尚未完成的 Segment 使用当前设置处理。
- 同一阶段允许出现来自不同设置指纹的结果。
- `inspect` 和阶段命令必须报告混合设置。
- 用户只有在明确使用 `--force` 时才重做选定范围。

## 1.3 非目标

MVP 不实现：

- 远程、多用户或公网 Web 服务。
- 消息队列或分布式执行。
- 同一项目的多个写任务并发运行或并发写入合并。
- 网络共享盘协调。
- EPUB 以外的复杂文档格式。
- Python LLM Adapter、自动 Provider 判断或通用工作流引擎。
- 远程插件、自动安装、插件市场或插件沙箱。
- Repository、Service、依赖注入或迁移框架。
- 无人工确认即覆盖术语库的自动冲突裁决。
- 自动翻译质量评分。
- 源 Segment 的增量编辑。
- TXT 字节级往返、原编码复刻或换行符保真。
- 面向未来版本的完整数据迁移能力。

---

# 2. 项目、输入与配置

## 2.1 最小技术栈与职责

运行时第三方依赖：

```text
httpx
chardet
fastapi
uvicorn
python-multipart
keyring
psutil
```

React/Vite/TypeScript 只用于构建随 Python 包分发的 Web 静态资源。标准库负责
CLI、异步调度、SQLite/JSON/TOML、Hash、日志、路径、原子替换、插件发现、
EPUB ZIP/XML 处理和 Unicode 归一化。

代码只需覆盖以下职责，不预先规定模块数量：

- CLI 和配置加载。
- 项目初始化及模板同步。
- TXT/EPUB Document Adapter、外部 Document Adapter 与 Segment 化。
- SQLite 项目持久化与项目外 JSON 交换文件。
- Prompt 渲染、Token 估算和 Chunk 生成。
- 声明式 LLM Adapter、HTTP 调用、限流和重试。
- 术语、翻译、校对、润色和 apply。
- inspect 与原文档格式导出。
- 默认只允许本机回环访问的 Web 工作台。

## 2.2 项目内容

项目至少包含：

```text
project.sqlite
config.toml
prompts/
input/
（项目元数据、File、Segment、Adapter 状态、术语、阶段结果、Run 索引和活动任务
状态均在 project.sqlite 中）
runs/{run_id}/manifest.json
logs/app.log
output/
```

Run 的配置和 Prompt 快照放在对应 Run 目录。启用调试模式时，该目录额外保存 Chunk、Attempt 和 Payload。

内置只读资源位于应用目录。全局配置修改、自定义 Prompt/Adapter/Preset、默认项目和诊断日志
写入平台用户数据根目录，可用 `ANOTHER_LLM_USER_ROOT` 环境变量覆盖。

默认用户数据根目录：

- macOS：`~/Library/Application Support/another-llm-translator`。
- POSIX：`~/.local/share/another-llm-translator`。
- Windows：`%LOCALAPPDATA%\another-llm-translator`。

用户根存在同名文件时优先读取：config 整文件覆盖，Prompt/Adapter/Preset 按文件或 ID 覆盖。

```text
config/config.toml
prompts/terminology.zh-CN.middle.txt
prompts/terminology.en.middle.txt
prompts/terminology_decision.zh-CN.middle.txt
prompts/terminology_decision.en.middle.txt
prompts/translation.zh-CN.middle.txt
prompts/translation.en.middle.txt
prompts/proofreading.zh-CN.middle.txt
prompts/proofreading.en.middle.txt
prompts/polishing.zh-CN.middle.txt
prompts/polishing.en.middle.txt
llm_adapters/openai-compatible.json
```

应用只识别当前包名、命令、环境变量、插件入口、默认用户根和钥匙串服务。旧版本位置中的数据
不会自动发现、迁移或删除；如需保留，请用户自行处理。显式设置
`ANOTHER_LLM_USER_ROOT` 时，以该目录为准。

Web 编辑全局资源只写入用户根；内置资源删除明确失败，编辑内置资源等于在用户根
写入同名覆盖。默认项目根为 `用户根/projects`，诊断日志位于 `用户根/logs/`。

## 2.3 初始化与文件发现

TXT 支持目录或显式文件；EPUB Adapter 每次导入一个显式文件。已安装的外部 Adapter
也参与扩展名发现；SRT Adapter 支持 `.srt` 文件和目录。项目也可先创建
不预设格式的空项目：

```bash
python -m app.main init INPUT... --name PROJECT_NAME
python -m app.main init INPUT_DIR --recursive --name PROJECT_NAME
python -m app.main init BOOK.epub --document-adapter epub --name PROJECT_NAME
python -m app.main init BOOK.epub --document-adapter epub --adapter-option epub.ruby_mode=aozora --name PROJECT_NAME
python -m app.main init SUBTITLES.srt --document-adapter srt --name PROJECT_NAME
python -m app.main init --empty --name PROJECT_NAME
python -m app.main init --empty --name PROJECT_NAME --parent-dir PARENT
python -m app.main files-add PROJECT INPUT...
python -m app.main files-add PROJECT INPUT_DIR --recursive
python -m app.main files-add PROJECT INPUT --document-adapter ADAPTER_ID
python -m app.main files-add PROJECT BOOK.epub --adapter-option epub.ruby_mode=base_only
python -m app.main files-remove PROJECT FILE_ID...
python -m app.main files-replace PROJECT FILE_ID INPUT --dry-run
python -m app.main files-replace PROJECT FILE_ID INPUT --yes
```

规则：

- init 必须在输入和 `--empty` 中恰好选择一种。
- init 默认写入用户根 `projects/`；`--parent-dir` 在已存在、可写的明确父目录下
  创建项目。相对路径按当前工作目录解析，后续命令可直接使用项目绝对路径。
- 项目目录是自包含边界；选择外部位置不会移动或复制已有项目。
- 显式文件按参数顺序处理。
- 目录按完整 POSIX 相对路径进行大小写不敏感、数字感知的确定性自然排序；
  自然键相同时按完整路径字典序打破平局。
- TXT 未传 `--recursive` 时只读取目录第一层。
- 递归发现时忽略符号链接。
- 显式符号链接输入直接拒绝。
- 多个输入映射到同一导出相对路径时拒绝初始化。
- 目录输入保存相对目录树；显式文件使用 basename。
- 输入文件复制到项目 `input/`，后续阶段只读取项目数据。
- 每个 File 记录来源 `document_adapter_id`、版本和可选不透明状态位置；旧项目
  的项目级字段在读取时解释为各活动 File 的来源，下一次文件变更时规范化。
- 空项目不锁定格式，可混合追加不同 Adapter 支持的文件。省略
  `--document-adapter` 时按 Adapter 声明的扩展名选择；目录可发现所有受支持
  格式，不支持文件被忽略并汇总提示。不同 Adapter 的扩展名声明不得重复。
- 新 File 追加到活动顺序末尾。`next_file_sequence` 单调增加；旧项目缺少该
  字段时从活动 File ID 最大值初始化。删除后重新添加不会复用 File 或 Segment
  ID。
- Web 可提交全部活动 File ID 的唯一完整排列，将 `file_order` 规范化为从 1
  开始的连续值。重排保留 File/Segment ID、输入副本、Adapter 状态、历史结果、
  项目计数和 `next_file_sequence`；File ID 不表示活动顺序。
- `files-replace` 原位替换一个活动 File，保留 File ID、`file_order`、导出相对路径
  和 `stored_name`，用新内容解析得到的编码信息、Adapter 版本、Adapter 状态、
  `segment_count` 及新 Segment 列表更新源副本。替换默认沿用当前 File 的全部
  Adapter 选项，但用户可以在预览确认前显式修改；预览记录旧值、新值和变化键。
  Segment ID 是稳定的不透明身份，
  位置只由 `file_order` 与 `line_index` 决定；新增 Segment 的 ID 不要求按当前位置
  递增。
- 替换预览只在相同 `part_id` 内按 `source` 与有效 `model_source` 做保守精确顺序
  对齐。相同连续区域和唯一锚点间的确定匹配复用原 Segment ID；无法唯一判定的
  重复项、修改项和缺失项不复用。Part 重排可匹配，Part ID 改名不跨 Part 匹配。
- 替换保留匹配 Segment 的所有阶段进度；新增 Segment 从零开始。缺失、修改和歧义
  旧 Segment 只移出活动源，历史阶段结果、Run 与既有输出不清理。已发布术语库不
  自动删除；存在 running Run 或未发布/未丢弃术语候选时拒绝替换。
- CLI 先预览并确认；`--dry-run` 只输出预览，`--yes` 用于显式非交互提交。Web
  通过单次上传的进程内预览会话完成预览和确认；会话按目标 File 唯一，确认、取消、
  覆盖或服务退出时清理。
- 活动文件的导出相对路径按大小写不敏感比较；追加产生冲突时整体拒绝。
- 文件增删或重排发现任意 `running` Run 时整体拒绝，不自动中断。未知、重复、
  缺失或部分非法的删除/重排选择也必须在写入前整体失败。
- 增删和重排在项目写锁内提交；普通发布异常不得留下部分
  更新。移除会删除项目内输入副本；Run、SQLite 阶段记录、术语和既有输出保留。
- 被移除 Segment 的历史阶段记录仍是审计数据，但不再参与 inspect、指纹差异、
  结果复用、校验警告、过期建议统计或导出。
- 移除 File 时同时清除该 File 独占的 Adapter 不透明状态；重新添加生成新状态。
- Web 项目概览提供“删除项目”操作。删除必须明确确认，且项目不存在未完成 Run；
  确认后删除整个自包含项目目录（输入、Run、术语库、阶段结果和导出），不可恢复。
  删除外部项目时同时清理本机 Web 会话中的最近路径记录。

空项目以及只含空白 Segment 的项目仍可打开、inspect、编辑配置、Prompt 和
Adapter，并可人工导入导出术语。术语、翻译、校对、润色、run-all、apply 和
export 必须在创建 Run 或请求前快速失败，提示先添加含非空 Segment 的文件。

EPUB Adapter 只接受 OPF `package` 版本 `2.0` 或 `3.0`，将非 spine 导航资源和 NCX 排在正文前，再按 spine 顺序读取 XHTML 文本流；spine 内的 nav XHTML 保持原位置且不重复。EPUB 3 `properties="nav"` XHTML 的整个 `body` 可见文本，以及 NCX 的 `docTitle`、`docAuthor` 和 `navLabel/text` 文本都会形成 Segment。普通透明内联元素（例如 `span`、`em`、`strong`）中的相邻 `text`/`tail` 槽合并为一个 Segment；未知结构和 `br` 形成边界，槽之间的非空白文本以及内部空白按源文保留。

每个 XHTML/NCX 资源是一个 `part_id`，但仍属于同一个 EPUB File；原 EPUB 及不透明定位状态用于重建输出；导航链接、元数据、图片、CSS、字体和其他未翻译资源保持原样。ZIP 路径穿越、符号链接、重复路径、异常条目数/解压大小/压缩比、越界资源以及非法 XML 会被拒绝。

EPUB 3 XHTML 允许省略 DOCTYPE，或使用不含外部标识的 `<!DOCTYPE html>`；EPUB 2 XHTML 允许省略 DOCTYPE，或使用 PUBLIC `-//W3C//DTD XHTML 1.1//EN` 的 XHTML 1.1 声明。

NCX 允许省略 DOCTYPE，或使用 PUBLIC `-//NISO//DTD ncx 2005-1//EN` 与标准 `http://www.daisy.org/z3986/2005/ncx-2005-1.dtd` 声明；其他外部 DTD、实体声明和错误标识均拒绝。

外部 DTD 永不加载；实体声明、不匹配版本的 DOCTYPE、SYSTEM-only 声明和不支持的 PUBLIC 标识均拒绝。

完整 `<ruby>` 子树和紧随的尾文本作为文本流中的一个成员；它与同一文本流中的普通 `text`/`tail` 槽、其他 Ruby 按源文顺序组成一个 Segment。

新导入可选择 `aozora`（默认）、`short_xml`、`compact` 或 `base_only`；选项固化于 File 的 Adapter 状态，不是项目运行设置。`base_only` 完全删除 Ruby/reading；其余模式的用户文本均使用 `｜base《reading》`。普通设置修改不会追溯既有 File；`files-replace` 可在预览中以当前值为默认并显式修改选项。

`short_xml` 和 `compact` 只把 Ruby 分别作为 `<r><b>base</b><y>reading</y></r>` 和 `⟦R:base|Y:reading⟧` 提交模型，输出在存储前还原青空。更改既有文件的模式必须移除并重新导入，从而分配新的 File/Segment ID。

纯译文 EPUB 将整条译文写入该语义 Segment 的首个可用位置，清空其余普通槽并移除全部 Ruby；双语 EPUB 保留完整源句和 Ruby，只在整个 Segment 末尾追加译文。

使用 `ruby_mode=aozora` 时，模型可以省略译文中的 Ruby 标记和 reading，但 base 是正文，必须正常翻译，不能因位于 Ruby 中而照抄。

保留时须返回严格的 `｜已翻译base《目标语言适用reading》`，系统会在译文区域恢复 EPUB Ruby；reading 必须翻译或转写，无法适配目标语言时应去掉标记和 reading，仅返回已翻译 base。没有返回 Ruby 合法且不触发重试；不完整、嵌套、含 HTML 或跨行的形式保持普通文本。

`base_only` 不还原 Ruby。旧 EPUB 0.3 `parenthetical` File 仅作为兼容状态继续读取和导出，新导入不再提供该选项。EPUB Adapter 0.5 可读取 0.3/0.4/0.5 状态，不改写旧 File、Segment、locator 或阶段结果；旧 File 需重新导入才会增加目录 Segment。

文件替换仍会保留旧 File 的 `parenthetical` 作为默认选项；用户可在预览中改为现行 Ruby 选项。

Reading 完全由同一个 `·・ • ◦ ● ○ ◉ ◎ ▲ △ ﹅ ﹆` 组成时视为 Emphasis Ruby。相邻同符号 Ruby 和单 Ruby 的重复符号合并为一个 reading；普通文本、空白、块边界和受控内联格式边界会切断合并。

用户 source/result 使用合并后的青空；最终 EPUB 译文区按 Unicode 扩展字素簇展开为逐字 Ruby，空白保持普通文本。嵌套 Ruby、空读音和无法确定读音结构的输入会带 XHTML 位置拒绝。

EPUB 还支持独立的 `inline_format_mode`：默认 `plain` 不向模型暴露普通内联标记；`markers` 为符合 `inline_format_policy` 的标签生成无 attrs 的唯一成对标记，并把对齐文本保存为 Segment 的 `model_source`，而 `source` 保持净显示文本。

`tiered`（默认）要求语义关键标签保留、允许表现层标签整体省略；`strict` 要求全部源标签保留。翻译、校对、润色及参考上下文使用 `model_source`，术语使用净 `source`。

Adapter 在结果写入前验证标记的已知性、唯一性、闭合、嵌套和父子关系；失败沿用格式修复预算，模型标记不会直接作为 HTML 写入，纯译文继续保留原普通标签和 attrs 的空骨架。既有 File 的导入选项不会因普通设置变化而静默重切；文件替换属于受控重新导入，可以在预览确认中修改选项。运行选项随 Adapter 状态和阶段指纹保存。

SRT 外部 Adapter 只接受核心字幕结构：唯一正整数序号、严格的 `HH:MM:SS,mmm --> HH:MM:SS,mmm` 时间行和非空正文；序号不要求连续，正文可跨多行。每个 cue 是一个 Segment，序号和时间行写入 `opaque_state`。

单语导出替换 cue 正文，双语导出在同一 cue 中追加换行和译文；cue 之间使用空行并保留文件末尾换行。输入换行统一为 LF，BOM、原换行风格和输入字节不进入项目契约。HTML/ASS 标记作为普通正文交给模型，插件不解析或保证保留；译文包含空白分隔行时导出失败。

缺序号、点号毫秒和时间行尾定位参数等非核心变体会被拒绝。

项目创建后，`project.sqlite` 是项目元数据、File、Segment、Adapter 状态、术语、
阶段结果和 Run 索引的唯一真相。原始输入、配置、Prompt、Run 快照、调试 Payload
和导出文件仍是项目外围文件。

> [!CAUTION]
> 手工修改 SQLite 或项目 `input/` 均不受支持。需要修改源文时，请重新创建项目。

`ImportedFile.segment_part_ids` 是与 `segments` 对齐的可选导入字段；省略时宿主
填入单一 `document` part。新项目必须为每条 Segment 持久化有效 `part_id`；读取
缺少、为空或不合法的旧数据时快速失败并提示重新创建项目，不读取 locator 猜测或
写入迁移。

## 2.4 输入编码与 Segment 化

解码规则：

1. 存在 UTF-8、UTF-16 或 UTF-32 BOM 时优先采用对应编码。
2. 否则使用 `chardet`。
3. `GB2312` 和 `GBK` 统一按 `GB18030` 严格解码，`ASCII` 按 UTF-8 解码。
4. 首选编码失败时只尝试一次配置的 fallback。
5. fallback 仍失败时拒绝该文件，不使用 replacement decode。

解码后统一内部行分隔，再切分：

```python
text = text.replace("\r\n", "\n").replace("\r", "\n")
segments = text.split("\n")
```

这样可以保留文件首部、正文中的连续空行和尾部空 Segment。原换行符、BOM、末尾换行状态
和输入字节不进入项目契约，也不决定输出格式。

每个文件记录：

- `file_id`
- `file_order`
- `original_name`
- `stored_name`
- `encoding_detected`
- `encoding_used`
- `segment_count`
- `next_segment_sequence`

每个 Segment 记录：

- `segment_id`
- `file_id`
- `part_id`
- `line_index`
- `source`
- `is_empty`

ID 示例：

```text
F0001
F0001-S000001
F0001-S000002
```

## 2.5 项目配置

配置严格拒绝未知键。CLI 和执行流程继续从项目 `config.toml` 读取配置。本地 Web 不暴露完整 TOML 编辑器，而是读取和提交覆盖下列全部字段的类型化分组表单；服务端通过同一严格校验后原子写入规范 TOML。

非法类型、缺失或未知字段、非法字段组合、Preset 缺失及项目不存在对应 LLM Adapter 均在写入前整体拒绝，原配置保持不变。

```toml
[project]
target_language = "简体中文"
target_language_tag = "zh-Hans"
output_encoding = "utf-8-sig"

[input]
encoding_confidence_threshold = 0.60
fallback_encoding = "utf-8"

[llm]
preset = "default"
preset_terminology = ""
preset_terminology_decision = ""
preset_translation = ""
preset_proofreading = ""
preset_polishing = ""

temperature_terminology = 0.1
temperature_terminology_decision = 0.1
temperature_translation = 0.2
temperature_proofreading = 0.1
temperature_polishing = 0.3

[execution]
scheduling_mode = "ordered_by_file"
# parallel 模式的纯源文 reference_context 使用字符串数组；ordered_by_file 保留
# source/translation 对象以携带已完成的上文译文。

[chunking]
target_chunk_input_tokens = 11000
allow_split_oversized_segment = true

[context.translation]
enabled = true
previous_segments = 3

[context.proofreading]
enabled = true
previous_segments = 3

[context.polishing]
enabled = true
previous_segments = 3

[context.terminology]
enabled = true
previous_segments = 3

[terminology]
unicode_normalization = "NFKC"
case_insensitive = true
max_terms_per_segment = 100
alias_primary_collision = "merge"

[validation.translation]
validators = []
max_retry_attempts = 2
exhausted_mode = "fail"

[retry]
http_max_attempts = 6
format_max_attempts = 2
base_delay_seconds = 2.0
max_delay_seconds = 60.0
jitter_seconds = 1.0

[debug]
enabled = false
inject_429_every = 0
inject_500_every = 0
inject_timeout_every = 0
inject_invalid_json_every = 0
inject_missing_segment_every = 0
```

`terminology.unicode_normalization` 接受 `""`（关闭）或 `NFC`、`NFD`、`NFKC`、`NFKD`，作用于术语主名称、别名与匹配文本的归一化。`terminology.case_insensitive` 为 `false` 时不做 casefold，按原始大小写匹配。

两项设置共同决定扫描候选去重、导入合并、alias 冲突判定与翻译时匹配；关闭归一化后仍保留首尾空白裁剪。

翻译、校对和润色的术语匹配会为严格、非嵌套的青空 Ruby `｜base《reading》` 建立独立的 `base` 正文视图和 `reading` 视图：base 可跨相邻 Ruby 匹配连续正文，直接相邻 Ruby 的 reading 也会连续组合。

普通正文会切断 reading 组合，base 与 reading 不会互相拼接。因此 `｜漢《かん》｜字《じ》` 可分别命中“漢字”和“かんじ”，而 `｜漢《かん》A｜字《じ》` 不会把 reading 拼成“かんじ”。同一术语从两个视图命中时只注入一次。不完整、嵌套、跨行或含 HTML 的形式仍按原文匹配。

已发布术语库中的 `normalized` 键是持久化标识，配置变更后不做迁移：旧术语与 override 继续按原键生效，新配置只影响之后的扫描、导入与匹配。术语阶段指纹包含全部术语配置，变更后重新扫描会自然产生新 revision；翻译、校对、润色阶段不记录术语配置到指纹，复用旧 Run 的翻译结果不会重新匹配。

Preset 中的 `requests_per_minute = 0` 和 `input_tokens_per_minute = 0` 分别表示
对每个 API Key 禁用 RPM 和 ITPM 限速。两者可以独立禁用；ITPM 为 0 时也不参与
Chunk 目标及单请求 Token 上限判断。模型上下文窗口和 `max_parallel` 始终生效，
`max_parallel_per_key` 为每个 Key 的并发上限，必须是正整数。

单个 `credential` 引用中的秘密值按行解析为 API Key 列表；接受 LF、CRLF 及一个末尾
换行符，拒绝空行、重复 Key 和 Key 内空白。每次执行或续作只在内存中严格解析一次并
冻结列表，缺失或格式错误在首个网络请求前失败。Key 原文、摘要和运行时健康状态不写入
项目、Run、日志或 Payload。

`llm.preset` 选择全局默认的 `llm_presets/<id>.json`；四个 `llm.preset_<stage>` 可用非空 Preset ID 覆盖对应阶段，空字符串继承全局值。Preset 再选择全局 `llm_adapters/<id>.json`；项目不保存 Adapter 副本。

Adapter 定义是完整的 Header/body 模板和成功响应 JSON Pointer；Preset 的 `extra_body` 可追加无冲突的 Provider 自定义字段。两者的内容 Hash 都进入阶段指纹，定义副本都进入 Run 快照，解析后的密钥不进入任何持久化内容。

完整 schema 见 `docs/ADAPTERS.md`。

Preset 的 `proxy_url` 为空时不显式设置代理，HTTPX 仍按默认行为读取 `HTTP_PROXY`、
`HTTPS_PROXY` 和 `NO_PROXY`。非空值只接受 `http://` 或 `https://` URL；
MVP 不增加 SOCKS 依赖。代理属于传输设置，不进入阶段指纹。

允许的调度模式：

```text
ordered_by_file
parallel
```

允许的翻译校验耗尽策略：

```text
fail
warning
```

## 2.6 全局模板同步

`init` 将全局配置和四个提示词复制为项目工作副本。项目配置和 Prompt 使用
项目副本；LLM 连接通过项目保存的 Preset ID 实时读取全局 `llm_presets/` 与
`llm_adapters/` 定义，不保存 Adapter 副本。

项目只记录：

```text
global_bundle_hash_seen
```

Bundle Hash 对全局配置和全部语言提示词（`prompts/<stage>.<lang>.middle.txt`）
的有效视图（用户根优先、内置兜底）按稳定相对路径及内容计算。它只用于发现新的
全局模板，不包含实时 Preset 内容，也不参与阶段结果判断。

项目命令执行前：

1. 全局 Bundle 与 `global_bundle_hash_seen` 相同：直接继续。
2. 出现新 Bundle 且为交互终端：展示变更摘要，询问更新或保留。
3. 选择更新：先备份当前项目配置和 prompts 到 `snapshots/template_updates/{timestamp}/`，再复制全局 Bundle，并记录新 Hash。
4. 选择保留：不修改项目副本，只记录新 Hash，避免重复询问。
5. 非交互环境：警告并保留项目副本，不更新 seen Hash。
6. 全局模板缺失或无效：不得覆盖有效项目副本。

`--dry-run` 只报告存在模板差异，不询问、不更新也不备份。

项目 Prompt 设置页另外提供按阶段和语言的精确同步状态。它只比较项目副本与当前有效全局 Prompt；“载入全局”仅更新编辑器草稿，不执行写入，仍需显式“验证并保存”。

用户也可以把草稿保存到用户级提示词仓库 `prompt_library/<stage>/<language>/<prompt-id>.middle.txt`，再按场景载入；仓库操作不会覆盖全局或项目 Prompt，也不参与项目 Bundle Hash、阶段指纹或 Run 快照。

---

# 3. 数据、进度与结果选择

## 3.1 schema v1 公共头部

内部 JSON 和 JSONL 记录使用 `schema_version = 1`。公共字段在适用时包含：

```text
schema_version
record_type
record_id
project_id
stage
segment_id
status
run_id
request_id
parent_record_id
created_at
```

阶段名、记录状态、错误类别、校对状态和 validation 状态使用集中定义的稳定字符串枚举。

读取器：

- 拒绝不支持的主版本。
- 允许 schema v1 出现未知的可选记录字段。
- 不要求外部 LLM wire payload 添加内部公共头部。

## 3.2 用户可见的三种进度状态

阶段进度只使用：

```text
pending
completed
failed
```

含义：

- `completed`：该 Segment 至少存在一条成功结果。
- `failed`：不存在成功结果，且最近一次尝试失败。
- `pending`：不存在成功或失败结果。

持久化阶段结果只写：

```text
completed
failed
```

`pending` 由缺少记录推导。缺少上游结果是阶段启动检查问题，不建立 `blocked` 状态。
设置变化只产生 warning，不建立 `stale` 状态。

结果选择始终查找最新 completed，而不是机械采用最后一条 JSONL 记录。同一 Segment 强制重做失败时：

- 旧 completed 继续作为当前可用结果。
- 最近失败仍可由 inspect 查看。
- 当前命令返回“选定范围未全部完成”。

普通阶段命令只处理没有 completed 的 pending 和 failed。只有 `--force` 会把选定范围内已有
completed 的非空 Segment 重新加入待处理集合。

## 3.3 阶段设置指纹

`stage_fingerprint` 是唯一用于设置差异提示的阶段 Hash。

它对规范 JSON 计算 SHA-256。规范数据包含：

- stage
- target language
- model
- 实际完整 Prompt 文本
- 当前阶段 temperature
- 当前阶段 context 配置
- `scheduling_mode`
- 影响阶段语义的配置
- 使用的 `terms_revision`，适用时
- LLM Adapter ID 与定义内容 Hash
- LLM Preset ID 与定义内容 Hash

翻译还包含启用的文字校验器 ID、插件及 Validator 版本和 `exhausted_mode`。
最大校验重试次数只影响执行，不进入指纹。

上述模型、Prompt、temperature、context、调度和术语字段适用于 LLM 阶段。apply 的指纹
只包含 apply 阶段、应用规则版本、建议类型和是否允许旧基准，不虚构模型或 Prompt 字段。

指纹不包含：

- Chunk ID、大小或组合
- Token 目标与模型窗口配置
- 并发、RPM、ITPM
- timeout
- HTTP、格式或校验重试次数
- Run ID

每条 completed/failed 结果记录本次 Run 的 `stage_fingerprint`。命令开始时：

1. 计算当前阶段指纹。
2. 汇总选定范围内已有 completed 所使用的指纹。
3. 不同且未使用 `--force` 时，必须由交互用户确认复用，或在非交互环境
   显式指定 `--reuse-mixed-fingerprints`。
4. 用户拒绝复用时停止且不创建本阶段 Run；使用 `--force` 才重做选定范围。
5. 新 pending 使用当前设置。
6. `inspect` 报告每个阶段使用过的指纹数量和当前指纹覆盖数量。

`--resume-run` 已经显式授权按当前设置续用旧 Run，不重复询问设置指纹。
`--dry-run` 不询问，只报告正式执行所需的选择。

不再计算 Request、Response、Prompt、Config、Source、Chunk Manifest、逐 Segment 术语或术语内容 Hash。

## 3.4 Run

Run Manifest 最少包含：

```json
{
  "schema_version": 1,
  "record_type": "run",
  "record_id": "RUN-20260729-120000-TR",
  "project_id": "PRJ-...",
  "run_id": "RUN-20260729-120000-TR",
  "stage": "translation",
  "status": "running",
  "stage_fingerprint": "sha256:...",
  "selected_segment_count": 215,
  "requested_segment_count": 180,
  "reused_segment_count": 35,
  "started_at": "2026-07-29T12:00:00+08:00",
  "completed_at": null,
  "created_at": "2026-07-29T12:00:00+08:00"
}
```

Run 状态允许：

```text
running
completed
failed
interrupted
```

Run 创建时复制项目配置；LLM 阶段另保存本阶段实际完整 Prompt、解析后的 `llm_preset.json` 和项目 `llm_adapter.json`。

续作不覆盖原快照，而是在 `continuations/0001/` 等顺序目录保存本次当前项目配置、Prompt、Preset 和 Adapter 快照，并在 manifest 追加本次指纹、原始范围和请求/复用数量。续作结果仍引用原 `run_id`。快照只记录 Preset 的 credential 引用，不读取或保存密钥值。

`terminology`、`translate`、`proofread`、`polish` 独立命令发现同阶段 `running` Run 时，最新一个是续作候选，更旧者标记为 `interrupted` 和 `superseded`。交互终端明确询问 `resume` 或 `new`；拒绝后候选标记为 `interrupted`，不再参与续作候选。

非交互运行必须显式使用 `--resume-run` 或 `--decline-run`。

续作保留旧 Run ID、原始 scope 和术语 `active_task_id`，忽略本次范围参数与
`--force`；它使用当前项目配置、Prompt、模型、端点及代理，只请求旧范围中
尚无 completed 的 Segment。再次中断时 Run 保持 `running`。

## 3.5 阶段结果

翻译 completed 示例：

```json
{
  "schema_version": 1,
  "record_type": "stage_result",
  "record_id": "REC-...",
  "project_id": "PRJ-...",
  "stage": "translation",
  "segment_id": "F0001-S000101",
  "status": "completed",
  "text": "翻译结果",
  "validation_status": "passed",
  "stage_fingerprint": "sha256:...",
  "run_id": "RUN-...",
  "request_id": "REQ-...",
  "created_at": "2026-07-29T12:01:00+08:00"
}
```

失败示例：

```json
{
  "schema_version": 1,
  "record_type": "stage_result",
  "record_id": "REC-...",
  "project_id": "PRJ-...",
  "stage": "translation",
  "segment_id": "F0001-S000102",
  "status": "failed",
  "error_class": "format_error",
  "error_message": "Missing translation field",
  "stage_fingerprint": "sha256:...",
  "run_id": "RUN-...",
  "request_id": "REQ-...",
  "created_at": "2026-07-29T12:01:02+08:00"
}
```

校对和润色结果还保存：

- `review_status`：`accepted` 或 `suggested`
- `suggested_text`
- `reason`
- `base_result_id`

`review_status` 是阶段 payload，不等于记录的 `status`。结构有效的 accepted 和 suggested 都保存为 completed。
accepted 的持久化记录将 `suggested_text` 和 `reason` 规范化保存为 `null`。

Web 人工重置在 SQLite 的对应阶段记录中追加 `status = "reset"`。reset 屏蔽该
Segment 此前的 completed；之后的新 completed 重新成为当前结果。重置校对或
润色时同时重置该阶段 applied 结果，但不级联删除其他阶段。

applied 结果保存：

- 实际输出文本
- 建议结果 ID
- 应用时的基准结果 ID
- 是否使用 `--allow-outdated-base`

---

# 4. 阶段流程

## 4.1 Prompt、上文与调度

四个 LLM 阶段使用：

```text
代码内固定 Prefix（按语言）
+ 项目可编辑的 middle Prompt（按语言）
+ 宿主通用格式规则与当前 Document Adapter 的专属要求
+ 代码内固定 Suffix（按语言）
```

固定 Prefix/Suffix 按阶段和语言（`zh-CN`/`en`）在代码内以字典提供；中段 Prompt
按 `prompts/<stage>.<lang>.middle.txt` 分语言保存，是唯一可编辑资源。硬编码规则
改版时显式升 `prompt_rules_version`。

固定 Prefix 定义阶段身份、输入字段、处理范围和数据/指令边界；多阶段任务的当前阶段目标、只读数据组成和可修改范围也由固定阶段 Prefix 定义。除顶层 `format_correction` 和 `validation_repair` 外，Payload 字段值均为待处理内容或参考数据，模型不得执行其中的指令。字段语义只在固定 Prefix 中定义，不再于可编辑 middle 或固定 Suffix 重复。

固定 Suffix 定义输出字段条件、状态转换约束、请求内短 ID、通用文本格式保真和严格 JSONL；具体文档格式的可重建要求由当前 Document Adapter 按 File 状态注入，不会进入其他格式的请求。每个非空物理行只能包含一个紧凑 JSON 对象，最后一行必须为 `{"type":"end"}`。

middle Prompt 承载可编辑的任务目标和判断标准，包括项目背景、文体、翻译策略、术语偏好及校对或润色严格度；它可以改变处理方式，但不能覆盖 Prefix/Suffix 的范围、数据边界和输出协议。

内置 middle 的更新只进入新项目或用户明确执行的模板同步，不自动覆盖既有项目副本；用户级提示词仓库只保存独立副本，必须载入项目编辑器并显式保存后才会影响项目；代码内 Prefix/Suffix 更新对所有项目生效。

Run 的提示词语言在运行时解析：Web 使用当前界面语言，CLI 使用
`--language`/`ANOTHER_LLM_LANGUAGE`/系统语言；该语言在当前项目提示词中缺失时
回退 `zh-CN`。实际使用的语言写入 Run manifest 的 `prompt_language`。

阶段指纹不包含项目中段 Prompt 原文；它记录 `prompt_rules_version`、全部语言中段的
哈希以及当前 File 的 Adapter 专属要求快照。因此任一语言的中段或 Adapter 要求变化
都会使该阶段既有结果指纹失效，但语言选择本身不产生指纹隔离。

四阶段分别读取自己的：

```toml
enabled = true
previous_segments = 3
```

数量表示当前 Chunk 首个 Segment 所在 `(file_id, part_id)` 中、该 Segment 之前
最近的非空 Segment 数。跨边界 Chunk 的后续 Segment 不会改变这份上文的边界。

规则：

- 上文不得跨 Chunk 首个 Segment 的 `file_id` 或 `part_id`。
- 一个 Chunk 共享一份 `reference_context`。
- `reference_context` 只携带理解所需的源文和可用目标文本，不携带 Segment ID。
- 上文不属于本次进度范围，不要求 LLM 输出。
- 失败或未完成 Segment 的源文仍可作为上文。
- 失败结果不能作为可信目标文本上文。
- 术语上文始终只含源文。

翻译、校对和润色请求把待处理 Segment 依次编号为 `"1"`、`"2"`……，模型只返回这些短 ID。
宿主在解析后映射回持久 Segment ID；只有合法且位于末尾的 `end` 才声明本批响应完整。
若该声明存在但返回 ID 的多重集合与请求不完全一致（缺失、重复或未知 ID），本批所有
候选都不可信，格式修正必须重新请求原批次；不能按返回顺序猜测或只补缺失项。
格式修正、校验修复、上下文拆分和超长 Segment part 请求各自重新编号。术语请求不发送
Segment ID。

诊断详情只在本次运行的有界内存中保留短 ID 到持久 Segment ID 的映射，摘要接口不传输
该映射。项目数据、阶段结果、普通日志和 debug manifest 只使用持久 Segment ID。

`ordered_by_file`：

- 默认同一 `(file_id, part_id)` 内 Chunk 顺序执行；启用跨边界合并时，调度器按
  每个 Chunk 包含的全部 File 保持这些 File 的 Chunk 不重叠。
- 不同文件可并发。
- 翻译、校对和润色上文可包含此前已有的阶段文本。

`parallel`：

- 所有 Chunk 可并发。
- 上文只含源文，不依赖当前 Run 尚未产生的结果。

术语提示词必须说明：参考上文只用于判断人物性别、指代、身份和上下文含义，不要仅因词语只出现在上文中就提取。

格式修正请求使用顶层 `format_correction`，只重新处理当前待处理内容并遵守固定输出协议；普通阶段只提供 JSONL 结构、固定字段和完整性等抽象指导，不携带上轮响应的具体错误。翻译校验修复使用 `validation_repair`，以每项 `failed_candidate` 为基准，只修复 `validation_matches` 所列问题并返回完整译文。

两类控制说明使用本 Run 的 Prompt 语言；英文 Prompt 不混入中文修正说明或中文解析错误详情。

## 4.2 术语

### 发布库与活动任务

术语目录只维护：

- 一份存放在 SQLite 中的已发布术语库。
- 一个 `active_task_id`。
- 追加式 scans 和 candidates。
- 一份人工 `overrides.json`。

已发布库包含递增整数：

```json
{
  "schema_version": 1,
  "record_type": "terminology_library",
  "record_id": "TERMS-7",
  "project_id": "PRJ-...",
  "terms_revision": 7,
  "published_run_id": "RUN-...",
  "terms": []
}
```

活动任务规则：

1. 普通 `terminology` 继续当前活动任务。
2. 没有活动任务且没有已发布库时，创建第一个任务。
3. 每个非空 Segment 在活动任务中记录 completed 或 failed 扫描。
4. 中断恢复只处理该任务中 pending 和 failed 的 Segment。
5. Chunk 变化不影响扫描进度。
6. 选定范围的设置指纹变化时先取得复用确认；pending 使用当前设置。
7. 所有非空 Segment 扫描 completed 后，合并候选、应用 overrides，并原子发布。
8. 发布成功后 `terms_revision` 加一。

`terminology --force` 创建新的全量活动任务，忽略此前扫描进度。上一份术语库在新任务发布前继续可用；发布时新候选合并到上一份术语库，未再次发现的旧术语不会自动删除。

术语“移除”通过人工 disabled override 完成；Web 还提供“彻底删除”，会同时删除术语记录及对应 disabled override，使后续扫描可以重新发现该术语。彻底删除不删除历史 scan/candidate 记录。

旧任务记录可以留在追加文件中，但不再参与产品逻辑，也不提供历史代次管理功能。

每条 scan 和 candidate 都记录 `active_task_id`；读取和发布时只使用 `active_task.json` 当前指向的任务记录。

### 候选和合并

术语请求不向 LLM 暴露 Segment ID：

```json
{
  "target_language": "简体中文",
  "reference_context": ["此前原文"],
  "source_segments": ["当前待扫描原文"]
}
```

术语扫描的 `reference_context` 和 `source_segments` 都是只含原文的字符串数组。扫描范围与
Segment ID 由程序内部持有；LLM 不需要也不得返回来源 Segment 引用。term 记录的 source
必须填写 `source_segments` 原文中实际出现的术语文本。

LLM 返回 JSONL；每个术语一行：

```jsonl
{"type":"term","source":"Alice","category":"女性人名","preferred_translation":"爱丽丝","aliases":["Ally"]}
{"type":"end"}
```

`source` 和 `category` 必填；`description`、`preferred_translation` 和 `aliases` 可选。人物类别尽量在上下文证据充分时标明性别，无法可靠判断时使用不带性别的类别。LLM 不需要声明术语属于哪个 Segment。

合法术语行可以先保存为候选；只有所有行合法且最终存在 end 时，请求覆盖的每个 Segment 才记录扫描 completed。否则格式修正仍重试原请求范围。`end` 必须严格等于 `{"type":"end"}`；例如 `{"type":"type":"end"}` 仍拒绝，不自动修复或接受。

严格失败不会回滚已经解析的候选，失败 Segment 会记录安全错误分类，Run manifest 记录分类及数量。

活动扫描的合法候选可以在全量扫描完成前读取：`terms-export --source scanned` 或 Web 术语页的“导出当前扫描结果”只导出当前活动任务候选，不改动已发布库。

用户确认 `terms-publish-partial` 或 Web 的“发布现有结果”后，候选按现有去重、冲突和 override 规则写入 SQLite 中的普通术语库，不添加 partial 标记，立即可供翻译、校对和润色。

该操作只把当前活动扫描标记为 `partial_published`，保留 scans、candidates 和历史 Run；下一次扫描创建新的活动任务，不删除旧记录。

归一化按 `terminology.unicode_normalization`（`""` 表示跳过）和
`terminology.case_insensitive` 进行：

```python
if unicode_normalization:
    value = unicodedata.normalize(unicode_normalization, value)
if case_insensitive:
    value = value.casefold()
normalized = value.strip()
```

候选以 normalized source 去重：

- 合并 aliases 和相同说明。
- 推荐译名或类别冲突时保留冲突信息，MVP 不自动裁决。
- 未解决冲突可以注入来源、候选类别和说明，但不注入歧义推荐译名。

alias 与另一条术语的主 source 相同时，由
`terminology.alias_primary_collision` 决定：

- `conflict`：保留两条主术语并记录 `group_claims`，争议文本只注入上下文，
  不注入任何争用方的推荐译名。
- `merge`（默认）：保留双方为独立条目，将被声明条目的 `group_primary` 指向
  声明者所在组的主条目；类别、说明、译名和普通 aliases 不跨条目合并。

循环 alias、多个术语争用同一主条目或连接两个既有组无法安全自动组化，始终
进入人工冲突。每个成员直接指向存在且启用的组主；悬空、链式和循环指针快速失败。

人工换主在单次项目写锁内重写全组。组主仍有成员时不能移除或永久删除；成员移除时同时脱组。

将尚不存在的 alias 物化为成员时，不复制主条目的译名、类别或说明。若同 normalized 的条目
已有可恢复的 disabled override，则物化会恢复该条目，保留其 source、译名、类别、说明和
aliases，并加入当前组。

术语组副条目可以在组页通过“退出组”解除关系。退组只写入显式 `group_primary = null` override，保留该条目的 source、aliases、译名、类别和说明；组主及其他成员不受影响。组主不能直接退组，需要逐个让副条目退出。

若原 alias 仍与退组条目的 source 碰撞，退组不会删除 alias，而是按当前 collision policy 生成待裁决的 `group_claims`；显式独立 override 会阻止后续自动重新组化。

人工 override 以 normalized source 定位：

```json
{
  "normalized": "alice",
  "category": "女性人名",
  "preferred_translation": "爱丽丝",
  "description": "人工确认",
  "aliases": ["Alice"],
  "group_primary": null,
  "disabled": false
}
```

override 在自动合并后应用。`disabled = true` 的术语不发布、不匹配也不注入。

发布时可以按确定性排序重新分配只在当前库内有效的记录 ID，不承诺跨 revision 稳定。

Web 术语组页可按 source 和 aliases 的严格包含关系推荐可能相关条目。推荐只供人工定位和确认，不参与自动组化；一字符被包含文本默认不推荐。确认后可以将候选加入术语组，或将候选 source 与 aliases 一并转为当前条目的 aliases 并以 disabled 方式移除候选。

快捷操作会在项目写锁内重新验证关系；两个已有组、未裁决 group claim、有成员候选或外部主条目 alias 冲突时整体拒绝。

组页中的副条目也可以直接转为组主别名；此操作不要求 source 之间存在包含关系，
会将副条目 source 与 aliases 合并到组主，保留其他成员关系，并以 disabled 方式移除
该副条目的独立译名、类别和说明。相关推荐中的“快速移除”复用可恢复的 disabled
移除语义；有成员的组主仍不可移除。

### 自动术语决策

自动术语决策是独立、显式触发的 `terminology_decision` LLM 阶段，不属于 `run-all`。它只审查当前已发布且启用、没有人工 override 的术语；override 仅作为只读一致性锚点。

宿主一次扫描 Segment 收集 source/alias 命中 Segment 数和最多五个上下文样本；`hit_count` 是至少命中一个 source/alias 形式的 Segment 数，不是字符出现次数。

样本先保留最多五个不同 `(file_id, part_id)` 内容边界的首个命中，再以其余不同 Segment 按源文顺序补满，因此单边界项目同样最多可提供五条样本。随后系统进行分批裁决和跨术语一致性复核。

两个阶段共用项目的可编辑决策规则，但使用各自的固定阶段指令。第一阶段 anchors 是人工决定；第二阶段 anchors 包含人工决定，以及第一阶段 disposition 已确定、当前启用且没有任何未解决冲突的自动状态。

第一阶段或已完成第二阶段检查点的 `needs_review`、disabled 及仍有冲突的状态不得锚定其他术语。所有 anchors 始终只读且不得输出 decision。模型只能保留、更新、软移除或标为 `needs_review`；不能修改 source/normalized 或虚构 alias。

固定 Prefix 定义输入证据、冲突候选、样本边界引用和两阶段 anchors 的含义；固定 Suffix 定义输出协议和状态转换约束。项目可编辑中段只定义判断政策，不得重新解释输入字段或改变协议。每个 action 都必须提供非空 `reason`。`keep`、`disable`、`needs_review` 只能输出 `type`、`normalized`、`action`、`reason`；`update` 还必须输出 `changes` Patch。

Patch 只能包含实际修改的 `category`、`description`、`preferred_translation`、`aliases`、`group_primary`；宿主将它应用于输入状态并生成完整 `after`。

空 Patch 只允许第二阶段显式解决第一阶段 `needs_review`，或重新启用当前 disabled 术语。固定协议允许 `description` 保留、清空或改写为简洁的目标语说明；非空改写必须由当前说明、源文样本或可见 anchor 支持，不得增加无证据事实。内置可编辑中段默认将 Description 视为简洁的术语区分说明，而非扫描观察或历史说明的汇总；重复、并列堆积、矛盾或泛泛的说明应压缩为一条有区分力的说明，无法提炼时清空。这是可编辑的默认判断政策，不增加宿主长度、分号数量或重复度校验。

宿主校验类型、空值归一化和 no-op，语义正确性由用户在草案中查看完整旧文本和新文本后确认。alias 只能选用输入中可见的既有源文形式。

LLM 的 `terms[]` 和 `anchors[]` 在对应术语存在证据时携带可选、只读 `conflicts`，结构沿用 `categories`、`preferred_translations`、`alias_primaries`、`group_claims`。

去重候选是证据，不是投票统计或允许值白名单；全文证据支持时模型可以提出候选之外的新值。第一阶段存在类别或推荐译名冲突时不得 `keep`；`update` 必须为每个冲突标量提供非空决议，也可以 `disable` 或 `needs_review`。`conflicts` 禁止出现在 decision 输出中。

格式修正请求携带去重后的 JSONL 结构指导、仍需修正的语义错误、上轮无效记录、已接受项和本轮唯一目标；结构指导不包含上轮响应的行号或原文。语义错误保留稳定的 `code`、`normalized` 和必要结构字段，纠错说明按 Prompt 语言生成；宿主内部错误文本只用于最终失败摘要，不直接混入其他语言的模型请求。完整校验通过的无关关系组件不会重复请求。连续出现相同错误时，各未决硬关系组件独立修正，但组件内部不可拆分。批级 JSONL、end 或未知记录错误仍使该次请求整体未决。

alias 转移必须是完整的多术语关系操作：接收方必须是启用的根术语，原所有者必须释放该 alias、被禁用，或在 source 转移时直接成为接收方成员。

单边新增其他术语仍持有的 source/alias、规范化后重复 alias 和把自身 source 作为 alias 都会被宿主拒绝；跨批次才能确认的关系冲突会按暂定状态重建。最终仍未解决的 alias/组争用会恢复整个依赖组件的运行前状态并列入 `needs_review`，不会产生隐式归组或部分建议。

第二阶段的 `terms` 和 `anchors` 输入携带只读 `disabled`，`terms` 还携带只读的第一阶段 action/reason；该字段不得出现在 decision 输出中。

第二阶段 `keep` 保留第一阶段 disposition 及理由，只有显式 `update`、`disable`、`needs_review` 才覆盖；第二阶段 `needs_review` 恢复运行前状态。`disable` 软禁用，`update` 应用 Patch 并启用术语。

两个阶段分别按当前 Preset 的 `max_parallel` 有界并发，第一阶段全部完成并形成统一暂定状态后才进入第二阶段。每个完整校验通过的批次原子写入 Run 检查点；用户取消或进程中断后可续用同一 running Run，已经完成的批次不再请求，未完成批次使用当前配置和 Prompt。

续作剩余第二阶段前，宿主将已完成的第二阶段检查点叠加到第一阶段暂定状态，重新计算每个术语的有效 disposition 和冲突，再用这一不可变快照生成 focus、可信 anchors 和关系校验状态；当前并发兄弟批次不会动态互相影响。当前决策规则版本为 7。

源术语 revision 或规则版本已变化时，在创建 continuation 前拒绝续作；规则版本 6 的 running Run 必须由用户显式结束并强制新建，不提供双协议兼容路径。中断 manifest 只记录安全错误码、原因码、最后 request ID 和完成步数，不保存 Prompt、响应或术语正文。

强制重做会结束旧 Run 并忽略检查点。检查点不是草案，不能审核或应用。

完整结果保存为绑定源术语 revision、模型和 Prompt 指纹的待处理草案。单术语建议可整条拒绝，分组和 alias 转移等多术语建议作为不可拆的组合建议。

应用前重新校验 revision、规则版本、未裁决标量冲突、override 保护、重建后的 alias 碰撞和组拓扑；无关 description、alias 或组修改不得借完整 override 清除冲突。

通过后在一个 SQLite 事务中写入确认后的 overrides、术语库新 revision 和 Run 状态；全部拒绝不增加 revision。草案保留应用前快照，只有应用后术语 revision 未再变化时才允许以新 revision 严格撤销。失败或替换失败不会留下部分草案，也不会丢弃原草案。

旧规则草案仍可查看、保存拒绝项或丢弃，但应用时要求重新生成。

审核界面使用术语页内的全宽工作区。建议默认接受，用户可以按类型、接受状态和文本筛选，冲突候选和关系争用也参与搜索。Proposal 与 `needs_review` 保存对应的只读冲突证据；界面展示历史类别、推荐译名候选、alias 归属、组关系争用，以及 Description 的完整新旧文本。

`needs_review` 不参与自动应用；应用或全部拒绝完成后，它们进入持久人工待办队列。队列状态写入对应 Run manifest，旧 Run 缺少处理字段时按未处理读取；同一 normalized 以最新完成 Run 的人工项为准。

存在待处理草案时，旧队列继续保留但在审核工作区中暂不开放，避免用户同时处理两个决策世代；启动新一轮决策前必须确认，只有新草案成功应用（或全部建议被拒绝完成）后才会替换旧队列，生成失败、取消、丢弃或替换失败均保留旧队列。

用户可从队列定位术语编辑或关系编辑，并显式标记或恢复“已处理”，该状态不受后续术语 revision 变化影响。

持久证据中的每个样本包含 `file_id`、`part_id`、`segment_id`、命中形式及 `source` 上下文片段；片段围绕实际命中位置截取，并标记为普通 source、Aozora base 或 Aozora reading 视图。

构造一次 LLM 请求时，focus 与 anchors 的可见样本共用连续的请求内 `boundary_ref`：相同 `(file_id, part_id)` 使用相同编号。

模型样本只包含 `boundary_ref`、`source`、`match_view` 和 `matched_forms`，不包含三个持久定位 ID；编号不表示全局 ID、顺序或权重。Token 预估和实际请求使用同一投影。草案、人工待办和审核接口仍保存完整定位；旧草案缺少 `part_id` 时仍可查看。

Anchor 使用 `compact` 策略时只移除样本，不改变按 Segment 计算的命中计数。

### 术语交换

`terms-import` 和 `terms-export` 只接受 `.json`、`.csv`。JSON 顶层固定为 `record_type = "terminology_exchange"` 和 `terms`。当前导出 `schema_version = 2`；继续读取 v1，且 v1 条目一律按独立主条目处理。

术语字段为 source、preferred_translation、category、description、aliases、disabled、可选的 `group_primary`，以及类别和推荐译名冲突候选。交换格式中的 `group_primary` 是组主 source，导入后按目标项目规则归一化。

CSV 导出包含同名列；导入只接受旧版或新版完整表头。数组字段保存为 JSON 数组字符串，导出编码为带 BOM 的 UTF-8。

导入在完整校验后按 normalized source 合并到自动扫描基线；文件缺项不删除
现有术语，人工 override 始终优先。`disabled = true` 是显式人工移除；
`disabled = false` 不自动撤销项目中已有的 disabled override。无实际变化时
不增加 revision。

### 翻译时匹配

程序对每个 Segment 的源文做相同归一化，然后用主名称和 aliases 进行子串匹配。

注入优先级：

1. 主名称命中。
2. alias 命中。
3. 更长的词优先。
4. 有推荐译名者优先。
5. 每个 Segment 最多 `max_terms_per_segment` 个术语组。

命中成员时注入组主和实际命中的成员，成员额外携带 `primary_source`。翻译、校对和润色请求中，每个注入条目的 `aliases` 只包含当前请求覆盖的 Segment 中实际命中的 alias；主名称命中但没有命中 alias 时为空。未直接命中的组主不携带 alias，同组多个成员命中时组主只出现一次。

批量请求中同一条目的多个 alias 命中时，`aliases` 使用去重并集。术语库中保存的完整 aliases 不受影响。截断先按组执行再展开，因此不会拆散组。

不持久化 occurrence 文件或逐 Segment 术语 Hash。每次构建翻译请求时重新确定匹配术语。

## 4.3 翻译

翻译输入最少包含：

```json
{
  "target_language": "简体中文",
  "reference_context": [
    {"source": "...", "translation": "..."}
  ],
  "terms": [
    {
      "source": "Silver Knight",
      "preferred_translation": "白银骑士",
      "category": "人物称号",
      "description": "银发骑士的称号"
    }
  ],
  "segments": [
    {"id": "1", "source": "..."}
  ]
}
```

`segments` 始终保留短 `id` 和 `source`，因为宿主需要将 JSONL 响应映射回持久
Segment。`ordered_by_file` 的 `reference_context` 使用带 `source` 的对象，并在可用时
携带 `translation`；`parallel` 的纯源文上文使用字符串数组以减少输入开销。

输出 JSONL：

```jsonl
{"type":"segment","id":"1","translation":"..."}
{"type":"end"}
```

映射规则：

- 只接受本次请求范围内从 `"1"` 开始的短 ID；宿主负责映射回持久 Segment ID。
- 不根据返回顺序或数量猜测对应关系。
- 有合法末尾 `end` 且 ID 多重集合完全匹配请求时，按字段校验结果保存有效项。
- 有合法末尾 `end` 但 ID 多重集合不匹配时，丢弃本次全部候选，使用
  `format_max_attempts` 对原批次整体重试；耗尽后原批次中仍未完成的 Segment 记录
  `format_error`。
- 没有合法末尾 `end` 时，保存已解析且有效的项；后续请求只包含未决 Segment，已保存项
  不回滚。未知或多余 ID 不会被映射。

> [!WARNING]
> 没有已发布术语库时允许直接 translate，但必须醒目警告，并在 Run 中记录
> `terms_revision = null`。`run-all` 会先完成术语任务再翻译。

### 翻译文字校验

校验器通过 ID 列表独立启用；内置校验器也通过可信 Python 插件注册：

- `japanese_kana`：Hiragana、Katakana、Katakana Extensions、半角片假名及 Kana 扩展块。
- `korean_hangul`：Hangul Syllables、Jamo、Compatibility Jamo 和扩展块。
- `source_text_residual`：先检查去首尾空白后的完整原文，再检查经 NFKC 和空白折叠后的保守长片段残留。
- `preferred_term_usage`：由独立的可信术语校验插件提供；只检查宿主实际匹配且带
  推荐译名的术语是否至少在候选译文中出现一次。该校验是 advisory，默认关闭。

长片段必须至少包含 12 个非空白字符、占源文非空白内容至少 30%，并包含 Unicode 字母。
纯数字和标点不触发。该校验器默认关闭。

校验发生在结构解析成功之后、写 completed 之前。

校验器上下文只包含当前 Segment 的源文、候选译文和宿主确定的逐 Segment 术语命中；
不向插件暴露项目路径、术语库对象或 Run。术语命中包含术语主名称、实际命中形式、
主名称/alias 类型和推荐译名。普通翻译请求中的 `terms` 形状和内容不因校验器改变。

命中时记录：

- 校验器名称和 `error`/`advisory` 强度。
- 硬校验的命中字符、Unicode code point、字符位置和候选文本（若有）。
- advisory 术语建议的术语主名称、实际命中形式和推荐译名。

普通模式只在最终 failed 或 warning 结果中保存必要校验信息；调试模式保存每轮候选和请求血缘。

修复流程：

1. 汇总当前轮校验失败或建议 Segment。
2. 按当前阶段的 Chunk 边界配置分组；默认限制在同一 `file_id + part_id`，启用
   跨边界合并时遵守不同 File 直连、同 File 跨 part 中间区间全为空的规则。
3. 正常非空行或筛选边界中断分组。
4. 修复请求只包含失败 Segment、源文、失败候选、命中字符、相关术语和允许的上文。
5. 超过 Token 限制时继续拆分。
6. 每轮修复后重新执行全部已启用校验器。硬校验使用配置的最大修复次数；每个
   Segment 的 advisory 术语建议最多只发起一轮修复，模型可以因语境不适用而保留
   原候选。

耗尽后：

- `fail`：保存 failed，候选不成为当前翻译。
- `warning`：保存 completed 和 `validation_status = "warning"`，允许进入下游，但 inspect 和导出必须报告。

术语 advisory 即使 `exhausted_mode = "fail"` 也不会单独把 Segment 标记为 failed；
一次建议修复仍未采用时保存 completed 和 warning。若同一候选同时存在硬校验问题，
硬校验仍按原有 fail/warning 规则处理。

## 4.4 校对

启动前检查选定范围内全部非空 Segment。任何 Segment 缺少 completed 翻译时，整个校对命令不创建 Run 并返回未完成范围。

输入包含：

- 原文。
- 当前最新翻译。
- 相关术语。
- 允许的同文件上文。
- 校对 Prompt。

输出 JSONL：

```jsonl
{"type":"segment","id":"1","status":"accepted"}
{"type":"segment","id":"2","status":"suggested","suggested_text":"完整建议译文","reason":"遗漏否定含义"}
{"type":"end"}
```

accepted 表示无条件保留当前基准，模型输出无需包含 `suggested_text` 和
`reason`；端点若返回这两个字段，本地无论其值和类型如何均直接忽略，并在
持久化时规范化为 `null`。suggested 必须包含非空字符串 `suggested_text`，
`reason` 只允许字符串或 `null`。

每条结果保存本次使用的翻译 `base_result_id`。

## 4.5 润色

启动前为每个选定非空 Segment 选择基准：

```text
最新 proofreading_applied
→ 最新 translation
```

任意 Segment 没有可用基准时，整个润色命令不创建 Run。

输出结构与校对相同，状态只允许 accepted 或 suggested。每条结果保存 `base_result_id`。

如果没有应用校对，润色直接以翻译为基准。`run-all` 因为不隐式 apply，所以校对和润色建议默认都独立基于翻译。

## 4.6 apply

命令：

```bash
python -m app.main apply PROJECT --stage proofreading --all
python -m app.main apply PROJECT --stage polishing --all
```

`--all` 是必需的批量确认，可以与文件或 Segment 范围组合。

应用规则：

- suggested：输出完整 `suggested_text`。
- accepted：输出建议记录所使用的基准文本。
- 不覆盖翻译或建议历史，写入 SQLite 中独立的 applied 记录。

apply 前必须确认建议的 `base_result_id` 仍是该 Segment 当前选择的基准。若上游已经变化：

- inspect 和普通阶段只警告。
- apply 默认拒绝整个选定范围。
- `--allow-outdated-base` 可显式强制，并在 applied 结果和日志中记录警告。

选定范围内任何非空 Segment 缺少 completed 建议或对应基准时，整个 apply 不创建 Run，也不应用可用子集。

## 4.7 run-all

默认顺序：

```text
术语
→ 翻译
→ 校对建议
→ 润色建议
```

不隐式 apply。

规则：

- 没有已发布术语库时，先完成活动术语任务。
- 存在未完成的活动术语任务时，先继续并发布，再翻译。
- 已有完整术语库且没有活动任务时直接复用。
- 设置指纹不同时逐阶段确认复用；拒绝后停止，不自动重做。
- 任一阶段选定范围未完成时停止，不启动依赖它的后续阶段。
- `--force` 明确要求所选阶段重做；用于术语时创建新的全量活动任务。

---

# 5. 执行可靠性

## 5.1 Token 与 Chunk

完整渲染 Prompt 必须满足：

```text
safe_estimate(rendered_prompt)
<= context_window_tokens
   - context_safety_margin_tokens
```

`max_output_tokens` 是提交给 Chat Completions 的输出上限，不作为每次请求必然
占用的固定预留量。每次请求实际发送：

配置为 `0` 时不发送输出上限字段；配置为正数时，下面的公式决定每次请求实际发送的上限：

```text
effective_max_tokens
= min(
    max_output_tokens,
    max(
        1,
        context_window_tokens
        - safe_estimate(rendered_prompt)
        - context_safety_margin_tokens
    )
  )
```

发生自动收窄时，本 Run 记录 warning，但不因配置的输出上限过大而拒绝启动。

Chunk 目标同时受 `target_chunk_input_tokens` 约束。估算必须覆盖：

- 固定 Prefix/Suffix。
- middle Prompt。
- 目标语言。
- 上文。
- 术语。
- 当前 Segment。
- JSONL 记录结构字符。

`token_safety_factor` 必须大于 `0`，可以小于、等于或大于 `1`。小于
`1` 会主动降低启发式估算值，可提高分词效率更高模型的上下文利用率，但也会
增加服务端上下文超限或实际 ITPM 超限风险。该估算不保证与任一模型、语言或
Prompt 的真实分词结果一致，使用者应依据实际分词器和端点行为调节。

每次尝试加入 Segment 后，重新渲染并估算完整 Prompt。普通 Run 创建后才规划下一批 Chunk，
调度器只保留与 `max_parallel` 同阶的有界缓冲。

Chunk ID 在进入调度时生成；调试模式随生成追加 Chunk Manifest。取消后不再继续规划。
Chunk 参数只影响本次请求组合，不影响任何已完成 Segment。

`target_chunk_input_tokens` 是软目标。贪心累计时，加入下一个 Segment 将超过目标便结束
当前 Chunk。单个 Segment 自身超过目标但仍低于模型输入硬限制时，可以单独发送。
文件末尾和范围末尾的短尾 Chunk 允许明显低于目标。

配置或请求在发送前失败的情况：

- 固定 Prompt 已超过硬限制。
- 上下文窗口无法容纳输入和安全余量。
- 单个实际请求的预测输入 Token 超过 ITPM。
- 单 Segment 即使拆到最小 part 仍无法容纳。

超长单 Segment：

1. 优先按段落类标点切分。
2. 再按句号、问号、感叹号。
3. 再按逗号、分号。
4. 最后按字符数硬切。
5. part 仍属于原 Segment，结果按顺序合并后只写一条 Segment 结果。

关闭 `allow_split_oversized_segment` 时直接记录该 Segment failed。

## 5.2 HTTP、并发与限流

请求 URL 固定由项目配置组成：

```http
POST {base_url}{endpoint}
Content-Type: application/json
```

Header、完整 JSON body 和成功响应正文路径由选中的 JSON LLM Adapter 定义。

内置 `openai-compatible` 使用 Bearer API Key、Chat Completions body、正文路径 `/choices/0/message/content` 和可选推理路径 `/choices/0/message/reasoning_content`、

`/choices/0/message/reasoning`；启用 SSE 时同时接受对应的 `/choices/0/delta/reasoning_content` 和 `/choices/0/delta/reasoning`。

另内置 `anthropic`、`google-gemini` 与 `openai-responses` 定义：分别使用 `messages_format` 消息形状转换、Preset `endpoint` 的 `${model}` 占位符与 `/output/-1/content/-1/text` 响应路径。

声明式 Adapter 默认使用非流式 JSON POST。schema 2 Adapter 可声明 SSE `streaming` 规则；只有 Preset 的 `stream = true` 时才使用流式 Endpoint 和流式请求 body。

普通 Preset 迁移后保持 `stream = false`，四个内置 Adapter 的非流式 body 与此前一致。

Adapter 可声明可选的 `models` 规格与 `usage` 映射。`models` 由 Web 在用户手动触发时以非流式 GET 检测连通性并读取模型列表，用于填写 Preset；不自动判断 Provider 或切换端点。探测使用当前 Preset 草稿并严格校验，但不保存草稿。

模型 ID 始终允许手工输入；发现结果只在当前列表中搜索，选择后仍需显式保存。

`usage` 把端点响应中的消耗换算为 input/output/ total 规范化计数，宿主在任务内累计端点实际返回的消耗，写入任务摘要与 Run `manifest.json`；任一成功或失败尝试未返回完整 usage 时，任务标记为 partial，保留已观测计数但吞吐量不可用；完全没有可观测 usage 时才显示 unavailable，不使用本地估算。

同一 Run 的续作累加各次精确回报；缺少累计版本标记的旧 Run 或任一次回报不完整时，Run usage 标记为 partial；完全没有可观测 usage 时才显示 unavailable。

项目的全局 `llm.preset` 及四个可选阶段覆盖实时解析全局命名 Preset。每个阶段只解析自己的覆盖或全局默认，不增加其他继承层。Preset 提供 Adapter ID、URL、模型、credential 引用、代理、Token 能力、每 Key 限速、两级并发、超时和 `extra_body`。

`extra_body` 必须是 JSON 对象，可以包含嵌套对象和数组；宿主在 Adapter 完整 body 渲染后追加其顶层字段。任何顶层字段冲突、模板占位符或缺失 Adapter 都在创建 Run 或发送请求前失败，不覆盖、不递归合并、不自动 fallback。

`run-all` 在同一项目内按阶段逻辑顺序执行，并可在该次调用内按资源键复用 HTTP Client 和
Key 调度状态；Run 快照与阶段指纹始终记录当前阶段实际使用的 Preset 和 Adapter。Web 的
不同任务不共享 HTTP Client 或连接池，只按 `(preset_id, preset_hash)` 共享每个 Key 的
RPM/ITPM 窗口、冷却和两级并发；相同 ID 但不同内容 Hash 的 Preset 相互隔离。Key 选择
按最早可发送者并在平局时轮转；等待额度、冷却或退避时不占 HTTP 并发槽。401/403 仅在
本次执行中隔离当前 Key，429 冷却当前 Key 并轮换，400/404 和协议/配置错误直接失败；
不同 CLI 进程不协调。Web 请求预览显示最终 body，并以 `***` 脱敏认证 Header。

Preset 内容进入 Run 快照和阶段指纹，因此其中不得保存密钥。项目配置、全局配置和 Run 快照都必须包含 `llm.preset`；内联连接字段和缺失 `llm_preset.json` 的 Run 续作直接失败。

整个命令共享一个 `httpx.AsyncClient`：

- 非流式请求的 connect、read、write 和 pool timeout 都使用
  `request_timeout_seconds`；流式请求默认把它作为连接及连续读取的空闲超时，
  `stream_read_timeout_enabled = false` 时仅取消连续读取超时，建连、写入和连接池
  等待仍受该值限制。流式请求不限制整个生成总时长。
- 连接池上限从 `max_parallel` 派生。
- `asyncio.Semaphore` 控制并发。
- 显式代理使用 `proxy=proxy_url`；空值不关闭 HTTPX 的标准环境代理。

RPM 和 ITPM 对每个 Key 使用独立的单进程 60 秒滑动窗口。相同 `(preset_id, preset_hash)`
的任务共享窗口、冷却及两级并发，检查与预约由同一个异步锁保护；释放后至少保留 60 秒，
且不得早于未到期冷却清理。每次实际 HTTP 尝试都重新预约额度，失败后不返还。

RPM 大于 0 时，每个 Key 的实际尝试还会按 `60 / RPM` 的最小间隔串行预约发起许可。首个
尝试立即预约，HTTP 请求发出后释放该许可，避免启动时突发。RPM 为 0 时不启用该节奏；
ITPM 为 0 时不参与 Token 窗口；两者都为 0 时仍受总 `max_parallel` 与单 Key
`max_parallel_per_key` 限制。

## 5.3 重试与部分响应

HTTP 重试：

- 408、5xx、连接错误、读取超时和普通流中断沿现有退避消耗一轮重试。
- 429 优先使用数值型 `Retry-After`；本轮健康 Key 全部限流时消耗一轮并等待最早冷却，
  否则在同一轮换用尚未尝试的健康 Key。
- 401、403 在本次执行中隔离当前 Key 并立即换 Key；全部 Key 隔离则失败。
- 400、404、协议或配置错误直接失败，不通过换 Key 掩盖。
- 所有 HTTP 错误共享 `http_max_attempts` 总上限。
- 退避使用有上限的指数退避和 jitter。

启用流式时，宿主要求响应为 UTF-8 SSE，并严格处理 CRLF/LF、注释、多行 `data:` 和任意 HTTP chunk 边界。

Adapter 的 `terminal` 声明显式终止方式：OpenAI-compatible 使用 `[DONE]`，OpenAI Responses 使用 `response.completed`，Anthropic 使用 `message_stop`，Gemini 使用最终 `finishReason`。

Adapter 可额外显式设置 `streaming.allow_clean_eof=true`，此时 HTTP 2xx、至少收到一个合法事件且自然到达 body EOF 也算传输终止；缺省为 false，其他内置 Adapter 仍要求显式终止事件。

EOF 前的所有事件都会被读取，尾部 usage 不会因缺少 `[DONE]` 而丢失。宿主在后台聚合正文、reasoning 和声明的 usage，只有终止后完整结果通过现有格式解析与校验才持久化。

首事件前超时、读取超时、未启用 clean EOF 的 EOF、流内服务错误或 HTTP 可重试错误会清空本次聚合并沿 `http_max_attempts` 重试，不隐式改发非流式请求；部分流中断可能产生重复计费。

Provider 可能在外层 HTTP 200 的 SSE 中报告最终错误（例如 `finish_reason=error`、上游状态 504）；这类事件按流内错误处理，不会进入格式修复或保存半成品。

诊断和 Debug 同时保留实际 `http_status` 与可选的 `provider_error_status`，避免把两层状态混为一谈。SSE 协议损坏、UTF-8/JSON 错误、匹配事件字段缺失或类型错误属于配置/协议错误，立即失败。用户取消立即关闭连接且不重试。

格式修正：

- Adapter 提取出的 content 开头允许存在一个完整的 Tag 思考块：`<think>...</think>`、`<thinking>...</thinking>`、`<thought>...</thought>` 或 `<analysis>...</analysis>`。

  Google AI Studio 兼容端点实测使用 `<thought>`。允许思考块前有 BOM 或空白；剥离后再按下述 JSONL 规则解析。
- 只剥离开头一个完整的已知思考块。未闭合、重复、嵌套或不在开头的标签不得
  猜测或全文删除，按普通格式错误处理；JSON 字符串字段内的同名文本保持原样。
- Adapter 还可配置 `response_reasoning_content_pointer`，或用有序的 `response_reasoning_content_pointers` 候选数组，提取字符串或 null 的结构化思考字段。

  候选路径缺失时继续尝试，首个存在的 null 规范化为 null，字段存在但类型错误时快速失败，不猜测或拼接多个字段。规范化响应包含 `content` 和可空的 `reasoning_content`；结构化字段与内嵌块同时非空时快速失败，不猜测合并顺序。
- 思考正文只存在于当前请求生命周期，不属于 Prompt、Chunk、Segment 结果或
  进度。普通模式不持久化；debug 模式仍只在原始响应 Payload 中保存，不新增
  独立思考记录。
- 原始正文或受支持 Markdown 围栏内部必须是一行一个 JSON 对象；围栏外说明文字忽略。
- 接受 `jsonl`、`ndjson`、`json` 和无标签围栏，不接受旧顶层 JSON 对象或数组协议。
- 每行独立解析；只有合法末尾 `end` 且 ID 多重集合完整时，才把响应视为完整批次。
- 存在合法末尾 `end` 但返回 ID 缺少、重复或带未知值时，整批候选作废并重试原批次；不
  根据返回位置修正关联。
- 缺少或提前 `end`（没有合法末尾 `end`）时，已解析且有效的 Segment 立即保存，后续
  格式修正只请求未决 Segment；空行不切断，已成功的非空 Segment 会切断其两侧未决项。
- 术语响应只有在无行级错误且以 end 结束时才推进扫描完成状态。
- 格式修正最多 `format_max_attempts` 轮。

模型报告上下文过长时，Chunk 对半拆分；单 Segment Chunk 进入内部 part 切分。

翻译文字校验使用自己的 `max_retry_attempts`，不与 HTTP 或格式重试相加。

## 5.4 持久化与中断恢复

项目内进度记录使用 `project.sqlite` 的事务、外键、唯一约束和 WAL。

项目数据库包含明确的 `schema_version`；当前项目存储 schema 为 v3：File、阶段、术语和 Run 索引字段保存在关系列中，`payload_json` 只保留无法由关系列重建的业务字段，Segment 完全关系化且不再有 `payload_json`。

缺失或未知版本快速失败；v1/v2 项目会在打开时事务迁移到 v3，不提供 JSONL 到 SQLite 的迁移或双写。Run 的可读 `manifest.json`、配置和 Prompt/Preset/Adapter 快照仍保存在对应 Run 目录，数据库中的 Run 索引负责活动任务发现和恢复判断。

迁移不会自动执行 `VACUUM`；需要回收 SQLite 空闲页时，使用 CLI `optimize`、Web 项目操作或对应 API 显式压缩单个项目。

项目外的普通 JSON（全局配置、Preset、导出交换文件等）使用同目录临时文件写完后
原子替换。`--dry-run` 不写入项目数据库，以保持零写入。

恢复只读取数据库中的：

- 每个 Segment 是否存在 completed。
- 没有 completed 时最近是否 failed。
- 活动术语任务中的 Segment scan 状态。

Segment 进度恢复不读取 Chunk ID、Chunk Manifest 或 Request 状态。Run 状态
只用于发现可续作的执行身份和旧 scope；实际 pending 仍完全由 Segment 结果或
术语 scan 推导。

## 5.5 普通日志与调试模式

普通模式保存：

- Run Manifest 和快照。
- SQLite 中的 Segment 阶段结果、术语任务进度与候选。
- 人类可读 `app.log`。

CLI 无论是否启用 debug，都会将带时间、级别和阶段的实时日志写入 stderr，并把相同基本
日志写入 `app.log`。最终命令汇总单独以 JSON 写入 stdout。日志不得包含正文、译文、
Prompt、API Key 或完整 Payload。

本地 Web 将相同安全摘要写入应用级 `logs/app.log`，按大小轮转，不因项目切换而清空；同时在有界内存中保留当前进程的结构化日志供仪表盘读取。仪表盘为当前进程的每个活跃 Web 任务独立保留逻辑 LLM 请求；HTTP 重试只追加到同一请求。请求摘要只存在当前进程内；所有活跃请求详情持续可读，已结束请求详情在所有会话间共享最近最多 200 条窗口。

每项保存规范化 messages、完整成功响应的 Content 和 Reasoning、模型、状态、HTTP 尝试次数、传输方式、流式事件数、接收字节数、首事件延迟、状态码和延迟。

单条 message 和 Content 最多 100,000 字符，Reasoning 最多 20,000 字符，超限时在详情中明确标记截断。

仪表盘无过滤时聚合当前进程全部活跃 Web 任务：请求、HTTP 错误、重试和等待数直接求和，
延迟把筛选后的所有 HTTP 尝试作为一个样本集计算（包括成功、429、其他 HTTP 错误和网络错误），
平均延迟为算术平均，P95 使用确定性的 nearest-rank 规则。项目或阶段过滤同时作用于日志、
请求和指标；已结束会话不计入当前指标。请求列表中的 `latest_latency_ms` 仍表示该逻辑请求最后一次尝试的延迟。

这些请求详情可能包含 Prompt 和源文；流式请求在完成前不会暴露增量正文，只能通过当前进程的详情接口按请求 ID 读取；诊断摘要轮询不返回正文。新 Run 开始时清空，应用重启后丢失，不经过普通 logger，也不会额外写入轮转日志、Run 文件或项目数据。

内存详情不采集 Header、API Key、Adapter Wire Body 或 Provider 原始 REST JSON；解析失败和终止错误只记录安全错误类别。显式启用 debug 时既有调试 Payload 仍按下述规则独立保存，不复用仪表盘的内存详情。

普通模式不保存：

- Chunk Manifest。
- 逐 Attempt 结构化日志。

每次执行或续作收尾时，Run manifest 追加一条 `key_audits`：包含凭据引用、执行序号、Key
数量，以及每个 `Key #N` 的逻辑请求数、实际尝试数、鉴权错误数、429 次数和该 Key 实际
收到的 usage。跨 Key 的同一逻辑请求分别计数，不把它们伪装成一个全局请求；缺失 usage
标记为 `partial` 或 `unavailable`，不使用本地估算。审计不保存 Key 原文、摘要或跨执行
健康状态；正常成功、失败和取消均收尾写入，强制杀进程前不承诺保留。

诊断请求详情和 Debug Attempt 使用独立的实际发送序号，并同时显示 Key 编号与重试轮数；
同一逻辑请求重试时序号不重复。
- 完整请求、响应和错误 Payload。
- 未校验的流式增量正文或原始 SSE 事件。
- 中间校验候选。

`debug.enabled = true` 时额外保存：

- 当前 Run 的 `chunks.jsonl`。
- 每次 Attempt 的结构化日志。
- 每次实际请求、响应或错误 Payload。
- 流式 Attempt 收集到的原始 SSE `data` 事件；失败 Attempt 同时保存错误元数据。
- 格式修正、上下文拆分和校验修复的父请求关系。
- 每轮翻译校验候选。

保存 Payload 时必须移除 Authorization、API Key、Cookie 和 Set-Cookie。

故障注入只在 debug 模式生效，计数配置为 0 时关闭对应注入。

---

# 6. CLI、inspect 与导出

## 6.1 命令

```bash
python -m app.main init INPUT... --name PROJECT_NAME
python -m app.main init BOOK.epub --name PROJECT_NAME --document-adapter epub
python -m app.main init --empty --name PROJECT_NAME
python -m app.main init --empty --name PROJECT_NAME --parent-dir PARENT
python -m app.main files-add PROJECT INPUT...
python -m app.main files-remove PROJECT FILE_ID...
python -m app.main inspect PROJECT
python -m app.main terminology PROJECT
python -m app.main terms-import PROJECT terms.json
python -m app.main terms-export PROJECT terms.csv
python -m app.main terms-export PROJECT scanned-terms.json --source scanned
python -m app.main terms-publish-partial PROJECT
python -m app.main translate PROJECT
python -m app.main translate PROJECT --resume-run
python -m app.main translate PROJECT --decline-run
python -m app.main proofread PROJECT
python -m app.main polish PROJECT
python -m app.main apply PROJECT --stage proofreading --all
python -m app.main apply PROJECT --stage polishing --all
python -m app.main export PROJECT --stage translated
python -m app.main export PROJECT --stage proofread
python -m app.main export PROJECT --stage polished
python -m app.main export PROJECT --stage translated --bilingual
python -m app.main run-all PROJECT
```

通用范围参数：

```text
--from-file F0002
--only-file F0002
--only-segment F0002-S000123
```

三者互斥。

行为参数：

```text
--dry-run
--force
--allow-outdated-base
--allow-missing
--bilingual
--all
--resume-run
--decline-run
--reuse-mixed-fingerprints
--document-adapter
--empty
--parent-dir
```

语义：

- `--force`：重做选定范围内所有非空 Segment，覆盖普通 pending/failed 筛选。
- `--dry-run`：不写文件、不创建 Run、不更新模板、不调用 LLM。它会耗尽规划器，报告范围、
  设置警告、完整 Chunk 数和 Token 估算。普通 Run 不在启动前生成全部 Chunk。
- `--allow-outdated-base`：仅用于 apply，允许应用基于旧上游结果的建议。
- `--allow-missing`：仅用于 export，允许使用阶段回退。
- `--bilingual`：仅用于 export。
- `--all`：apply 的必需批量确认。
- `terms-import` 根据 `.json` 或 `.csv` 扩展名增量导入术语；`--dry-run`
  只校验并报告变化。
- `terms-export` 根据输出扩展名导出术语；默认不含 disabled，
  `--include-disabled` 用于完整备份人工移除决定；`--source scanned` 导出当前活动
  扫描中已经解析的候选。
- `terms-publish-partial` 在当前术语扫描未运行且存在候选时显式发布部分结果；Web
  端要求同样的确认，不提供自动发布或自动修复路径。
- `--resume-run`：用于四个主要 LLM 阶段和 `terms-decide`，续用最近同阶段 running Run。
- `--decline-run`：用于四个主要 LLM 阶段和 `terms-decide`，明确结束该候选并创建新 Run。
- `--reuse-mixed-fingerprints`：显式复用选定范围内设置指纹不同的 completed；
  仅用于四个 LLM 阶段和 `run-all`，并与 `--force` 互斥。
- `--document-adapter`：用于带输入的 init 或 files-add，显式选择输入 Adapter；
  init 默认 `txt`，空项目不保存该选择。
- `--empty`：仅用于 init，显式创建 0 文件项目，不能同时提供输入。
- `--parent-dir`：仅用于 init，在指定的现有可写父目录创建项目；省略时使用
  内置 `projects/`。
- `files-add`：追加源文件；省略 Adapter 时按已安装 Adapter 的扩展名识别，
  目录使用 `--recursive` 递归发现所有受支持格式。显式选择 Adapter 时由该
  Adapter 解释输入。
- `files-remove`：按 File ID 从活动项目移除文件，不清理历史结果。
- `export --file FILE_ID`：只导出指定活动 File；参数可重复。省略时导出全部
  活动 File，未知、重复或显式空范围在发布前失败。

两项续作参数互斥。没有候选时 `--resume-run` 是用法错误；
`--decline-run` 直接按当前范围创建新 Run。`--dry-run` 不询问、不修改
manifest；与 `--resume-run` 合用时按旧范围生成续作计划，但不写 continuation。

删除：

```text
--include-stale
--allow-stale
--update-from-global
--keep-project-version
```

稳定退出码：

```text
0    成功；允许普通 warning 和 validation warning
1    未分类内部错误
2    CLI 用法、配置或模板错误
3    项目、schema 或存储完整性错误
4    鉴权、端点等阶段级致命外部错误
5    选定范围仍有 pending 或 failed
130  用户中断
```

## 6.2 inspect

inspect 至少报告：

- 文件数、总 Segment 和空 Segment。
- 当前已发布 `terms_revision`。
- 活动术语任务的 completed、failed、pending。
- 活动术语任务的候选数量、失败分类和可导出的部分结果；严格 JSONL 失败不会丢弃
  已解析候选，部分发布后下游立即使用普通术语库。
- 翻译、校对、润色和 applied 的 completed、failed、pending。
- 当前设置指纹与已有结果指纹的差异及混合来源数量。
- 有旧 completed 但最近重做失败的 Segment 数。
- validation warning 数。
- 基于旧上游的校对或润色建议数。
- 当前仍为 running 的 Run ID、阶段、开始时间和原始范围。
- 是否存在新的全局模板。
- 建议的下一条命令。

inspect 不修改 Run 状态或用户进度。旧 JSONL 项目不做尾行修复，直接提示重新创建。

## 6.3 导出

阶段映射：

```text
translated → 最新 translation completed
proofread  → 最新 proofreading_applied completed
polished   → 最新 polishing_applied completed
```

输出目录：

```text
output/translated/
output/proofread/
output/polished/
output/bilingual/translated/
output/bilingual/proofread/
output/bilingual/polished/
```

尚未 apply 的 accepted/suggested 建议不能直接作为 proofread 或 polished 导出结果。

通用行为：

- 缺少选定阶段结果时停止并报告。
- validation warning 和混合设置必须出现在导出摘要。
- 默认逐 File 使用其来源 Document Adapter；`--format txt` 改用宿主内置 TXT
  导出。插件缺失、版本不兼容、状态损坏或能力不足时明确失败，不静默转换。
- 所有 Adapter 先在同一宿主临时目录生成并完成路径校验，再移动到正式目录；
  任一生成或校验失败时不发布输出。

`project.target_language_tag` 是输出文档的可选 BCP 47 语言标签，与供 LLM 使用的 `target_language` 文本名称分离。宿主在原格式导出时将两者传给 Document Adapter；具体 Adapter 决定是否应用或在标签为空时拒绝导出。

旧项目缺少该字段时按空字符串读取，不根据 `target_language` 推断。当前 EPUB Adapter 要求非空，TXT Adapter 忽略该字段。

TXT 按 `file_order` 和 `line_index` 重建，每个输入文件独立导出，并使用 `project.output_encoding` 严格编码。编码无法表示结果字符时失败，不静默替换。

EPUB 输出一个 `.translated.epub` 或 `.bilingual.epub`，使用 BCP 47 标签重写 OPF `dc:language`；双语文件把目标语言放在第一项并保留源语言。已重写的 spine XHTML 根元素同时更新 `lang` 和 `xml:lang`。

除此之外只重写翻译对应的 XHTML 文本单元及其定位槽位。普通复合 Segment 的单语译文写入首个槽并清空其余槽，保留原内联标签骨架；包含 Ruby 的复合 Segment 可以混合普通槽和 Ruby 槽，单语移除该 Segment 的全部 Ruby，双语在完整源句末尾追加译文。

只有旧的独立 Ruby locator 继续按其专用定位规则导出。

SRT Adapter 按 cue 序号和时间行重建 `.srt`，单语替换正文，双语在正文末尾追加
换行和译文；所有 cue 独立保留在同一 File 中。输出使用项目的
`output_encoding` 严格编码，无法表示结果字符时失败。

```bash
python -m app.main export PROJECT --stage translated --format original
python -m app.main export PROJECT --stage translated --format txt
python -m app.main export PROJECT --stage translated --file F0001 --file F0003
```

除各 File 来源格式（包括已安装的外部 Adapter）和 TXT 外，不提供任意格式转换。
文件范围同时适用于原格式和 TXT；只校验所选 File 的阶段结果。宿主保持
`file_order`，不提供按 Segment 导出或跨 File 合并；跨 File 合并只属于启用配置的
LLM Chunk 请求规划。

`--allow-missing` 回退：

```text
translated → source
proofread  → translation → source
polished   → proofreading_applied → translation → source
```

所有回退 Segment 写入摘要。

单语模式：

- 每个源 Segment 对应一个目标文本逻辑行。
- 空 Segment 对应空行。

双语模式：

- 非空 Segment 先写原文，再写目标文本。
- 空 Segment 只写一个空行。
- translated、proofread 和 polished 都支持。
- 回退文本仍写在原文下一行。

TXT 可以使用任意一致的文本行分隔方式。验收不检查换行符种类、BOM、末尾换行
或输出字节，只检查逻辑行和可见空行结构。EPUB 验收检查 spine 顺序、XHTML
结构及未修改资源仍可读取。

## 6.4 本地 Web

`python -m app.web` 只允许绑定 `127.0.0.1` 或 `localhost`。HTTP 层拒绝非本机 Host 和跨站 Origin。Web 创建项目时不预选文档格式；待输入列表可分多次加入单独文件或文件夹，并可在同一项目中混合 Adapter。

文件夹输入保留内部相对路径，单独文件只保留 basename；大小写不敏感的重名使本次选择整体拒绝。文件夹内不支持的文件被忽略并汇总提示，单独选择不支持文件直接失败。项目概览使用同一输入队列追加文件，并可经典多选移除。

普通浏览器的每次文件夹选择、桌面壳提交的服务端文件夹和 CLI 递归目录均使用相同自然排序规则。项目概览用独立把手进行桌面拖放重排；拖动已选文件时，全部选中文件按当前顺序组成连续块移动，拖动未选文件时先改为单选。窄屏或粗指针设备提供单文件排序模式，通过置顶、上移、下移和置底按钮操作，不实现触屏长按拖动。

每次放置或按钮移动后立即提交完整 File ID 顺序，失败恢复操作前顺序。`POST /api/v1/projects/{name}/files/reorder` 接受非空 `file_ids` 数组并返回 `reordered_file_ids` 与 `file_count`；不提供 CLI 重排。

保存父目录和打开项目目录默认填入服务端绝对 `projects` 路径，用户可直接修改，也可通过服务端目录浏览器逐层选择。目录浏览器只列当前层目录，不递归扫描或返回文件内容，用 `...` 返回上级目录。Windows 驱动器根目录会列出逻辑驱动器；未装载或不可用的驱动器仍显示但置灰，可刷新后重新探测。

不支持 `webkitdirectory` 的浏览器明确禁用文件夹选择，但仍可选择单独文件。外部项目使用项目自身 ID 作为 Web 路由标识；路径规范化并去重，无效项目、不可写父目录和目标冲突在写入前失败。

Web 只在当前浏览器的版本化 localStorage 保存最近外部项目路径。页面加载时逐一向本机服务提交这些精确路径；不扫描父目录，不自动移动项目。失效路径会明确提示并从最近列表移除，默认 `projects/` 项目继续直接列出。

导出页用 Ctrl/Cmd/Shift 经典多选限定文件范围，未选择时导出全部。项目配置使用覆盖全部现有字段的分组表单；项目 Prompt 与 JSON LLM Adapter 在设置页分别提供高级编辑器。

项目 Prompt 编辑器展示当前阶段/语言与全局 Prompt 的同步状态，支持仅载入全局草稿，并提供不覆盖全局的用户级提示词仓库；载入仓库条目后仍需显式保存项目 Prompt。

Web 还提供全局配置、全局 Prompt 和 LLM Preset 管理；全局配置与 Prompt 只影响新项目或用户明确同步的项目，Preset 修改则立即影响引用项目。Web 还可运行/取消阶段任务、人工审校、apply 和 export。

Web 与 CLI 使用同一 SQLite 项目数据库、应用内核和持久化记录。同一项目的写任务通过非阻塞文件锁互斥，冲突时明确失败。

服务器配置的 `[tasks] max_active_projects` 默认是 2，必须是非布尔正整数，可在全局设置或
`PUT /api/v1/server/config` 修改；`GET /api/v1/server/status` 返回当前值。旧的
`server.toml` 缺少 `[tasks]` 时长期按 2 读取，下次保存设置时补写该节。

Web/桌面进程内每个项目最多一个活动阶段任务；所有项目共享全局项目槽位，默认最多 2 个。
额外任务进入内存 FIFO 队列，queued 不持有项目文件锁。槽位释放、启动时提高上限或任务完成后
立即提升最早任务；降低上限不抢占已经运行的任务。任务提升后才获取项目锁，并重新校验当前
running Run、阶段指纹以及 `force`/复用/续用选择；若项目在排队期间变化导致原选择不兼容，任务
明确失败，不自动改选。queued 可直接取消，running/cancelling 沿用 asyncio 取消流程；应用关闭
时取消队列和运行任务，已创建的 Run 按既有规则收尾为 interrupted。

WebTask 的临时状态和队列只存在于当前 Web/桌面进程；不持久化、不跨进程调度，也不从重启恢复
内存队列。浏览器刷新或关闭后，前端通过 `GET /api/v1/tasks/active` 重新发现仍为 `queued`、
`running` 或 `cancelling` 的任务；只对本页已经观察过而从 active 消失的任务再读取一次详情，
完成、失败和取消状态仅保留在本页会话。独立 CLI 进程不受全局项目槽位限制，多进程同项目冲突
仍由文件锁拒绝。

Web 进程重启不会恢复旧的取消句柄，但项目 Run 与 Segment 记录仍按既有机制保留业务进度。

Web 在版本化 localStorage 中保存最近选中的 `project_id`。页面重新打开时先恢复该项目（外部项目仍按已保存的精确路径重新打开），再将活动任务按稳定项目 ID 关联；项目失效时清理选择并回退到首个可用项目。

顶部保留当前项目的详细任务状态条；另有紧凑“任务 N”全局面板，列出当前进程所有
queued/running/cancelling 任务的项目、阶段、状态、完成/失败/待处理进度、输入/输出 Tokens
或不可用标记，并提供打开对应项目和逐个取消。面板和状态条在项目切换、导出、设置及窄屏布局中
均可用；前端只用一个轮询流程刷新全局 active 列表。

`cancelling` 状态不重复显示取消按钮；完成、失败和取消状态只保留在当前页面会话，重新打开时不从已结束 Run 重建。其他项目若有活动任务，在项目选择器显示 “运行中”徽标和数量提示；切换到该项目后，当前项目详细状态条随之切换。

复用 Segment 计入已完成，不显示 Combined Tokens；失败数量可点击跳转到当前阶段的错误筛选，错误行只显示稳定的安全错误分类和摘要。

Web 仪表盘不依赖当前项目，可查看当前进程活跃任务聚合后的请求并发数、平均请求延迟、P95 请求延迟、HTTP 错误、重试、当前限流等待请求数、累计输入/输出 Tokens 与总吞吐量；筛选项目或阶段时同步缩小日志、请求和指标范围。筛选中的活跃会话全部返回完整 usage 时，Token 求和且吞吐量可用；存在 partial usage 时显示已观测 Token、吞吐量不可用；完全没有可观测 usage 时，Token 与吞吐量均显示 unavailable。已结束会话不计入当前指标。

当前等待数包含本地每 Key RPM/ITPM 额度、单 Key/总并发排队以及 HTTP 429 的 Retry-After 或退避；网络错误、408 和 5xx 的普通重试退避不计入。日志支持级别、项目、阶段和文本过滤，并可暂停自动滚动。

吞吐量只使用完整端点 usage 除以当前运行耗时；partial usage 保留已观测 Token，但吞吐量显示不可用；没有任何可观测 usage 时，Token 与吞吐量均显示 unavailable。右侧请求/ 响应列表只轮询轻量摘要；双击请求或选择“查看”后，按需显示请求、Content、Reasoning 和尝试详情，运行中的已打开详情随仪表盘刷新。所有活跃请求详情持续可读；已结束请求详情在所有会话间仅保留最近 200 条，活跃请求不计入该上限。

完整详情可能包含 Prompt 和源文，但普通日志和指标仍不记录鉴权 Header、请求/响应 Payload、Prompt 或源文。

Web 页面使用顶层选择的中文或英文语言格式化界面、诊断时间和数字；项目内容、
Prompt、目标语言和模型输出不随界面语言改变。前端可见的业务错误和请求参数错误
均返回稳定的 `code` 与结构化 `params`，`error` 文本仅作为安全的 fallback。

0 个非空 Segment 时，阶段运行入口不可用，服务端预检也拒绝创建后台 WebTask。
文件移除确认必须说明源副本和活动 Segment 会删除，而历史阶段结果及既有输出
保留，重新添加会获得新 ID。

Web 只在术语、翻译、校对和润色页面提供阶段启动入口。每次启动前读取当前阶段统计和最近的 running Run：存在未完成 Run 时必须明确续用或结束后新建；存在不同设置指纹的 completed 时必须明确复用或 force。

force 与复用互斥，续用 Run 时不接受会被忽略的 force 或复用参数。所有决策在创建后台任务和阶段 Run 前再次校验；并发修改导致条件变化时明确失败，不自动降级。桌面和窄屏使用同一运行决策流程。

术语页支持 JSON/CSV 导入导出、经典 Ctrl/Cmd/Shift 多选和批量移除；活动扫描期间显示完成/失败/待处理、失败分类和候选数量，并可导出当前候选或在确认后部分发布。部分发布立即更新 SQLite 中的普通术语库，只结束活动扫描状态，保留历史扫描和候选记录，下一次扫描使用新的任务。

翻译、校对、润色列表使用相同多选规则；批量清除采用追加 reset。校对和润色可应用所选或当前过滤范围，缺建议或缺基准时整批拒绝，旧基准必须显式允许。批量清除不会改变阶段运行 scope；随后启动仍处理项目内全部 pending/failed。

## 6.5 凭据、局域网共享与桌面壳

### 凭据与 Preset 引用

LLM Preset schema v5 使用显式单凭据引用 `credential: {kind, name}`，并以 `stream` 与 `stream_endpoint` 控制可选 SSE：`environment` 读取指定环境变量，`keychain` 读取系统钥匙串；两者二选一，不隐式 fallback。`max_parallel_per_key` 为每个 Key 的正整数并发上限，`max_parallel` 仍为 Preset 总并发上限。

schema 2/3/4 用户 Preset 在启动 CLI、Web 或桌面 sidecar 时原子迁移到 v5，新增字段取旧的 `max_parallel`；Run 内历史快照只在内存中补齐默认值，不改写审计文件。v1 的 `api_key_env` 字段已移除，加载时明确拒绝并提示改用 `credential`。

密钥只在执行开始经 `resolve_api_keys` 解析，不进入 URL、请求正文、Run 快照或阶段指纹。

凭据以用户根 `credentials/index.json` 保存摘要（ID 与更新时间），密钥只存系统钥匙串。
凭据 API 支持创建、更新、删除和测试，创建/更新输入使用多行文本框且永不回传秘密；Run 和日志只保存引用 ID。模型发现请求必须携带从 1 开始的 `key_index`，只测试选中的 Key，越界直接失败，不自动切换。

索引原子写入，损坏时明确报错，不静默回退。测试环境通过 autouse 的 FakeKeyring 隔离，
绝不触碰真实钥匙串。

### 局域网共享与认证

服务器配置保存在用户根 `server.toml`：`lan.enabled`、`lan.bind_address`、`auth.required`、
`auth.username` 与 `[tasks] max_active_projects`。默认只允许本机回环访问；并发上限默认为 2，
必须是非布尔正整数。

CLI 与桌面模式统一监听 `0.0.0.0`，由中间件按请求守卫：非回环客户端在未启用共享时返回 `local_only` 403；启用共享后只放行所选接口网段内的客户端（`0.0.0.0` 表示全部网段），网段外返回 `out_of_subnet` 403，本机回环始终可用。

启用共享且开启认证但未登录时返回 `auth_required` 401。绑定地址必须是本机可用的非回环接口地址或 `0.0.0.0`（`/api/v1/server/interfaces` 用 psutil 枚举启用的接口，含 netmask），保存后即时生效，无需重启。

开启认证后使用长期用户名与密码，密码存入系统钥匙串；登录成功签发 HttpOnly、SameSite=lax 的会话 Cookie（30 天），会话保存在内存，重启或停止共享后全部失效，长期账密保留。回环访问始终免认证。停止共享时清除全部会话。

公开端点仅限 `/api/v1/server/status`、`/api/v1/server/interfaces`、登录/登出与非 `/api/` 静态资源。留空认证必须显式确认警告：同网段设备拥有完整项目和 LLM 操作权限；未认证共享时 Web 常驻显示该警告。

局域网共享使用 HTTP，不实现 TLS、多账号、角色或密码找回。

### 桌面壳（Tauri）

`src-tauri/` 提供 Tauri 2 桌面开发壳：启动时拉起 Python/FastAPI sidecar，对 `/api/v1/server/status` 做健康探测，通过后加载 `http://127.0.0.1:8765`，退出时关闭 sidecar。

原生文件与文件夹选择器经 `window.__TAURI__` 桥接为 `select_file` / `select_folder`，把服务端路径随创建/追加请求提交；普通浏览器和 LAN 客户端继续使用上传与服务端目录浏览。

`ANOTHER_LLM_PYTHON`、`ANOTHER_LLM_REPO_ROOT`、`ANOTHER_LLM_WEB_PORT` 仅开发模式生效。

---

# 7. 核心验收矩阵

## 7.1 输入与输出

测试数据包含：

- UTF-8、带 BOM UTF、GB18030 和可由 fallback 解码的 TXT。
- 多文件、自然排序、递归子目录和不同目录同名文件。
- 首部空行、连续空行、尾部空 Segment 和空文件。
- 编码无法表示目标译文的失败样本。

验收：

- 文件和 Segment 顺序正确，不跨 File 或文档 part 混合。
- 解码失败明确拒绝，不产生替换字符。
- 单语导出每个 Segment 对应一个逻辑行。
- 三个阶段的双语导出均遵守原文/目标文本规则。
- 未配置时使用 `utf-8-sig`，显式配置的其他输出编码生效。
- 输入编码不影响输出编码选择。
- 输出可由常用查看器正确显示。
- `--allow-missing` 回退和摘要正确。
- 不比较输入输出的换行符、BOM、末尾换行、编码或字节。

## 7.2 阶段流程

验收：

- 四阶段上文数量分别生效且不跨当前 Chunk 首 Segment 的 `file_id` 或 `part_id`；
  `cross_boundary_batching` 只改变允许合并的请求 Segment 边界。
- 术语上文只含源文，不计入扫描进度。
- ordered_by_file 和 parallel 的上文内容符合定义。
- 术语按 normalized source 合并，override 和 disabled 生效。
- 活动术语任务可续作，force 新任务完成前不替换已发布库。
- 失败扫描保留候选与安全错误摘要；当前候选可导出，显式部分发布只清除活动扫描
  状态并保留历史记录。
- 翻译 ID 映射严格，空 Segment 不请求。
- 日语假名和韩语谚文校验覆盖配置的 Unicode 块。
- 校对和润色只在完整上游范围存在时启动。
- accepted/suggested 与记录 completed/failed 正确分离。
- apply 默认拒绝旧基准建议，显式参数可以强制。
- run-all 不隐式 apply。

## 7.3 恢复、Chunk 与重试

验收：

- 默认每个 Chunk 只含同文件同 part、保持源文顺序的待处理非空 Segment；启用
  `cross_boundary_batching` 的阶段还覆盖跨 File 直连和同 File 跨 part 的空区间
  规则。
- Chunk 可以跨空行，但不能跨已完成、范围外或其他未处理的非空 Segment。
- 没有合法末尾 `end` 的部分响应保存中间 Segment 后，其两侧未决项重新拆成独立 Chunk；
  合法末尾 `end` 但 ID 数量不符时不保存任何候选，原 Chunk 整体重试。
- 修改 Chunk 大小、并发、限流或调度后不丢失 Segment 进度。
- 中断后 completed 不重复请求，pending 和无成功结果的 failed 继续处理。
- 四个独立 LLM 阶段能发现并续用相同 Run ID；旧 scope 和术语任务保持不变，
  当前配置与 Prompt 写入新的 continuation 快照。
- 拒绝候选后不再询问；非交互参数和 dry-run 零写入语义正确；多个 running
  Run 中更旧者被 supersede。
- 强制重做失败不遮蔽旧 completed，但命令返回退出码 5。
- 合法部分响应立即保存有效 Segment。
- 原始 JSONL、CRLF、BOM、空行、受支持 Markdown 围栏和已知开头思考块均可解析。
- 未闭合、重复、嵌套或不在开头的思考标签会进入格式修正，JSON 字段内标签文本
  保持原样。
- 结构化思考 Pointer 的字符串正常提取，null 或缺失路径归一化为 null，非法
  类型及其与内嵌思考块冲突快速失败；普通模式不新增思考持久化记录。
- 缺失、重复或提前 end、非法行会进入格式修正；合法末尾 `end` 下的缺失、重复或未知
  ID 会使原批次整体进入格式修正。
- 旧顶层 JSON 对象或数组不再接受。
- 无合法末尾 `end` 的格式修正和校验修复只请求连续分组后的未决 Segment；合法末尾 `end`
  但 ID 不完整时，格式修正请求原批次全部 Segment。
- HTTP、格式和校验重试均不会超过各自总上限。
- 单 Preset 多 API Key 按每 Key 独立 RPM/ITPM 和单 Key 并发调度，并受 Preset 总并发
  上限约束；401/403 隔离、429 冷却轮换、普通退避和错误分类符合第 5.2 节，收尾
  manifest 含逐 Key `key_audits` 且不泄露秘密。
- 显式 HTTP/HTTPS 代理传给 HTTPX；空代理保留标准环境代理行为，非法协议拒绝。
- 401/403 只隔离当前执行中的失败 Key；全部 Key 失效时停止尚未发送的任务。
- JSONL 尾行损坏可恢复，中间行损坏明确停止。
- debug 开关两种状态下 stderr 和 `app.log` 都有基本运行日志，stdout 仍是可独立解析的最终 JSON。

## 7.4 用户设置决策

验收：

- Prompt、模型、目标语言、上下文、校验策略或术语 revision 变化时，交互
  环境询问是否复用；非交互环境要求显式 flag。
- 用户确认或指定 `--reuse-mixed-fingerprints` 后已有 completed 继续复用。
- 新 pending 使用当前设置。
- inspect 能报告同一阶段的混合设置来源。
- 只有 `--force` 才重做已有 completed。
- Chunk、Token、并发、限流、timeout 和重试变化不进入阶段指纹。
- 调度模式会改变可用上文内容，因此进入阶段指纹并要求相同确认。

## 7.5 模板、普通模式与调试

验收：

- init 正确复制全局模板。
- 新全局 Bundle 在交互模式询问一次，更新前备份，保留时不覆盖。
- 非交互模式只警告且不改变 seen Hash。
- dry-run 对模板和项目均零写入。
- 普通模式不生成 Chunk、Attempt 和完整 Payload 文件。
- 调试模式保存 Chunk、Attempt、Payload 和父请求关系。
- 调试故障注入不会在 `debug.enabled = false` 时生效。
- 普通和调试模式都能通过 Segment 结果恢复。

## 7.6 Adapter、EPUB 与 Web

验收：

- JSON LLM Adapter 的类型化占位符、自定义嵌套字段、认证 Header 和 RFC 6901
  响应路径生效；非法 schema、未知占位符、缺失路径和非字符串正文快速失败。
- Adapter 定义副本与 Run 快照不包含 API Key，定义 Hash 进入阶段指纹。
- LLM Preset 的实时解析、嵌套 `extra_body`、顶层冲突和占位符拒绝生效；实际
  Preset 快照与内容 Hash 进入 Run 和阶段指纹，请求预览不泄露认证 Header。
- 四阶段可分别覆盖全局 Preset；同一项目 `run-all` 按资源键复用 HTTP Client 和 Key 调度状态，
  不同 Web 任务仅共享相同 `(preset_id, preset_hash)` 的每 Key RPM/ITPM、冷却和并发状态，
  不共享 HTTP Client；
  不同 Preset 内容使用独立资源，Run 与指纹记录实际阶段 Preset。
- TXT 旧项目没有 Document Adapter 字段时仍按 `txt` 导出。
- EPUB 保持 spine 顺序、跨节点 Segment 定位、导航、元数据和非翻译资源；
  纯译文和双语文件均可重新打开。
- `target_language` 与 `target_language_tag` 由宿主传给所有 Document Adapter；EPUB 单语以该标签作为唯一 `dc:language`，双语将其列为第一语言，并同步已重写 XHTML 的语言属性。

  EPUB 译文标题增加 `（目标语言）`，双语标题增加 `（目标语言·双语）`，同时使用稳定独立出版标识和更新后的修改时间避免阅读器缓存原书元数据。
- EPUB Ruby 与同一文本流的前后文合为语义 Segment，三种导入模式、纯译文移除
  全部 Ruby 和双语在完整源句末尾追加译文均生效；导入选项只固化在对应 File
  Adapter 状态。
- EPUB ZIP 路径、符号链接、压缩炸弹、非法版本化 DOCTYPE 和 XML 实体输入明确
  拒绝；普通内联文本合并、混合 Ruby 复合定位和旧独立 Ruby locator 均保持可导出。
- 独立 SRT 插件按 cue 建立 Segment，严格校验序号和时间行，纯译文与双语导出均
  保留 cue 元数据；HTML/ASS 标记不由插件解析，破坏 cue 边界的译文被拒绝。
- Document Adapter 缺失、版本不兼容、状态损坏或运行失败时不发布部分输出，
  也不静默回退。
- Python 插件发现拒绝重复 ID 和未知协议版本。
- Document Adapter 扩展名按大小写不敏感保持唯一；Web 待输入列表支持混合
  文件、文件夹相对路径自然排序、批次冲突阻止和不支持文件汇总；项目文件拖放
  重排持久生效，非法排列或运行中 Run 不改变原顺序。
- Web 只接受本机 Host/Origin，与 CLI 共用项目记录；同项目第二个写任务明确
  失败，取消后的 Run 有正确收尾。
- Web 项目配置表单覆盖完整配置 schema；非法类型或组合不改变原 TOML，保存
  后的规范 TOML 可由 CLI 原样读取。
- Web 可在没有打开项目时管理全局配置、Prompt 和 Preset；全局模板修改不改变
  现有项目，显式同步通过严格校验；内联 LLM 配置在读取边界直接拒绝。
- Web 仪表盘在项目切换和窄屏下可见全局日志、当前请求指标及精确 usage；过滤、
  搜索和自动滚动暂停生效，usage 不完整时不显示估算吞吐量；本次运行请求/
  响应详情可按需查看且不会由仪表盘写入磁盘。
- CLI 可在指定父目录创建并通过绝对路径打开项目；Web 可创建、打开、去重并
  恢复最近外部项目，且不会扫描父目录或移动项目。
- React 生产构建、TypeScript 检查、桌面与窄屏关键交互、浏览器控制台均通过。

---

# 8. 最终最小架构

```text
TXT / EPUB / 外部 Adapter（示例：SRT）
   │
   ▼
稳定 File / Segment
   │
   ├───────────────┐
   ▼               ▼
活动术语扫描阶段结果历史
   │               │
   └──────┬────────┘
          ▼
项目配置与 Prompt 副本
          │
          ▼
Run 原始快照与续作快照
          │
          ▼
同文件连续临时 Chunk
          │
          ▼
声明式 JSON LLM Adapter
```

最终原则：

```text
File 是内容边界。
Segment 是唯一进度和恢复单位。
Chunk 只是当前请求包装。
Run 记录一次执行。
设置变化不使旧结果失效；复用或重做由用户明确决定。
普通模式保存最小结果，调试模式增加请求审计。
TXT 保持逻辑行；EPUB 保持原包资源和格式定位；SRT 保持 cue 序号、时间行和正文边界。
```
