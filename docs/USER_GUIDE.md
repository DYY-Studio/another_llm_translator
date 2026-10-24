# 用户指南

本指南介绍 Another LLM Translator 的本地 Web、macOS 桌面壳和 CLI 操作。

建议一般用户优先使用 Web。macOS 桌面壳维护较少，CLI 适合自动化和精确控制执行范围。

## 1. 使用方式

### 本地 Web

通过浏览器提供项目、术语、翻译、校对、润色、设置和导出界面。

从源码启动需要 Python 3.11+ 和 Node.js/npm：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --prefix web
npm run build --prefix web
python -m app.web
```

打开 `http://127.0.0.1:8765`。使用 `python -m app.web --port PORT` 可以更换端口。

前端构建完成后，日常启动只需激活虚拟环境并运行 `python -m app.web`。

### macOS 桌面壳

桌面壳使用与 Web 相同的项目存储和执行逻辑，在独立应用窗口中运行，并提供原生文件、
文件夹和导出位置选择器。

要求 macOS 13 或更高版本，并自带 Python 运行时，无需另行安装 Python。

目前需要从源码构建，参见[开发指南](DEVELOPMENT.md#4-macos-桌面开发)。

### 插件

官方 SRT Adapter 和术语校验插件随应用提供。用户插件解压到 `<用户数据目录>/plugins/<插件目录>/`，重启应用后生效。

加载失败时按提示处理。插件要求见[Adapter 契约](ADAPTERS.md#3-可信-python-插件宿主beta)。

### 升级与迁移

打开受支持的 SQLite 项目时，应用会在后台升级存储并保留升级前备份，迁移失败会明确显示错误。

## 2. 配置模型连接与凭据

### 使用 ChatGPT Plan

1. 在主机本地打开`设置 → 全局设置 → ChatGPT Plan`。如需代理，先保存连接代理，再点击
**Continue with ChatGPT**。授权在系统浏览器中进行，完成后返回应用查看账户和 Plan 授权状态
2. 在 LLM Preset 编辑器选择 `chatgpt-plan`，发现并明确选择模型，再保存 Preset
3. 在项目或全局设置中选择对应 Preset 即可使用

#### 注意事项
* 由于目前 ChatGPT 未提供 OAuth 适用的用量接口，只能通过`管理用量`访问网页查看 ChatGPT 额度
* ChatGPT Plan 强制启用 SSE，不接受 Temperature
* 退出后可重新登录原账户，更换账户时先退出，再选择`连接其他账户`
* CLI 复用同一用户数据目录中的已保存授权，缺少有效授权时需先到本机设置页登录
* 额度耗尽或授权失效会停止任务并保留已有进度。处理额度或重新授权后，按现有恢复流程继续
* 通过可信局域网使用时，必须先登录主机配置的会话认证，才可查看连接摘要、发现模型和执行 Plan 任务，连接管理只在主机本地提供

### 使用 API Key
首次翻译前，至少需要一个可用的 LLM Preset。

1. 打开右上角“设置”。不需要先打开项目也可以编辑全局设置
2. 进入全局 Preset 管理，选择一个内置示例或创建同名用户版本
3. 选择定义请求端点适用的 Adapter，填写实际的 Base URL、模型和限流参数
     - RPM 和 ITPM 按每个 Key 独立计算。`总并发`限制整个 Preset，`单 Key 并发`限制每个 Key
     - 如果 Adapter 支持 SSE，可在 Preset 中开启`流式请求`
4. 选择一种凭据来源并填写，若希望使用系统钥匙串，请先在凭据管理中写入对应凭据
5. 保存后可以手动检测模型列表，并查看请求预览和诊断信息

#### 环境变量

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

这些 Preset 只是示例，使用前必须自行配置和核对。

#### 系统钥匙串

在`全局设置`的`凭据管理`中保存密钥（每行一个 API Key），再让 Preset 引用对应的钥匙串凭据。

#### 注意事项
* `发送温度参数`控制是否使用项目的阶段温度，不支持时应当关闭此项
* Reasoning Effort 从 Adapter 提供的等级中选择，`不发送`采用供应商默认行为
* 输出上限的下限由 Adapter 定义，Anthropic 要求大于 0。不要求输出上限的 Adapter 在设为 0 时不发送该参数。
* `JSON Schema`开关为声明了输出结构的任务启用结构化输出，目前只有 LLM Decision 使用
* 请求预算可在全局或项目设置中调整；项目预算字段可单独覆盖。内容目标控制请求如何分批，输入上限限制完整请求内容，预留值为模型输出保留上下文容量。参考资料也计入输入上限。若必要内容超出上限，可调整相应预算或 Preset 限流配置，并确保仍为输出留出足够上下文；不可拆分内容超限时会明确报告。
* 设置页显示未知配置字段的警告；保存后仅保留受支持字段。非法字段值会明确报错。
* 全局配置中的目标语言是新项目的默认值。现有项目拥有自己的配置副本，全局配置变化不会自动改写已有项目，需要在打开项目后进入`项目设置`修改
* 在配置页`格式修正次数`旁，可将`未解决 ID 重试范围`设为`整个 Chunk`，让翻译、校对和润色在缺失、重复或字段错误导致 ID 未解决时，丢弃本次部分结果并整批重新请求。重试仍受格式修正次数限制，设为 0 时不重试。

### 空响应重试

可在全局设置或项目设置的`空响应重试`中，分别选择限制截断和原因未知的重试方式与次数

Adapter 明确报告长度限制结束时，空响应归类为限制截断。其他空响应归类为原因未知。

自定义 Adapter 参见[响应边界](ADAPTERS.md#响应边界)

### 流式请求（Stream）

流式请求会持续接收 SSE 事件，以避免等待低速端点输出时超时。

Preset 可以显式关闭连续读取超时，但不会取消连接超时。

流式请求的协议和失败处理由 Adapter 定义，具体规则见[Adapter 契约](ADAPTERS.md#sse-流式规则可选)。

> [!WARNING]
> 流式请求不能解决上游迟迟不发送首事件或代理缓冲 SSE 的问题。
> 
> 连接中断时，已收到的半成品会被丢弃，并按既有重试次数重新请求，**可能造成重复计费**。

## 3. 创建和管理项目

### 创建项目

在项目选择菜单中选择“新建项目”，填写项目名、项目父目录并添加源文件。未选择文件时会创建空项目，之后可以继续添加内容。

项目使用创建时的全局目标语言默认值，创建后可在项目设置中修改。

- Web 浏览器模式通过上传或服务端目录浏览器添加文件。macOS 桌面壳使用原生文件或文件夹选择器。
- 一个项目可以包含多个支持的文件。
- 选择文件夹导入时，文件夹内按保留的相对路径自然排序，例如 `chapter2.txt` 位于
  `chapter10.txt` 之前，分批选择和单独文件的顺序保持不变。导入后可以手动重排。
- 运行阶段任务期间不能添加、移除或重排文件。

### 提示词管理
- 项目创建时会复制源文件，并生成独立的项目设置和 Prompt 副本。之后修改全局模板默认不会
自动改变现有项目，此时可以使用项目设置中的全局模板同步，或在项目 Prompt 页面进行操作。
`载入全局（未保存）`只替换编辑器草稿和预览，必须点`验证并保存`才会生效。
- 用户数据目录中的`提示词仓库`按阶段和语言保存命名模板，全局和项目 Prompt 页均可使用。载入仓库条目、`加载内置提示词`读取随程序分发的原始版本，都只更新草稿，需要手动保存生效
- 项目提示词`恢复继承`经确认后会删除项目覆盖并丢弃未保存内容，不自动备份
- Decision 提示词在 Prompt 页的 Decision 分组中编辑，内置提示词为英文，不随界面语言变动，可直接改写为其他语言并保存

### 打开和删除项目

项目可以从用户数据目录的项目列表打开，也可以通过绝对路径打开外部项目。此时打开的外部项目不会被移动。

项目有运行中任务时，仍可以切换项目、刷新页面并查看概览、术语、阶段结果和任务进度。运行中只能查看，文件变更、其他修改和项目修复需等待任务结束或取消后再操作。

`从项目移除文件`只移除文件，不会移除既有历史结果，而`删除项目`会永久删除整个项目目录，执行前应确认没有需要保留的数据或未完成的 Run。

#### 文件顺序
* 项目概览中的文件顺序也是后续浏览和处理的顺序，务必将其调整为阅读顺序。
* 桌面端拖动已选文件时，全部选中文件会按当前顺序一起移动并收拢为连续组。拖动未选文件时，只移动该文件。
* 窄屏或触屏设备可进入“排序”模式，选择一个文件后使用“置顶 / 上移 / 下移 / 置底”。
每次移动都会立即保存，失败时恢复移动前顺序。
* 重排不会删除已有译文、历史结果或该文件的导入设置。

#### 运行格式
* 具有运行格式设置的文件会显示`设置`
* 可以逐文件修改，或使用概览顶部的`统一设置`按 Document Adapter 对项目内该 Adapter 的全部当前文件部分覆盖
* 批量操作不会依据当前勾选的文件筛选
* 运行格式设置只影响之后新建的 Run，不改写原文、定位状态或既有结果
* 运行确认框会只读显示统一值或`存在多种值`，具体可返回概览调整

> [!WARNING]
> 请勿手工修改项目中的 `project.sqlite` 或 `input/` 内容。需要更新源文时，请通过应用替换文件。

## 4. 完整翻译工作流

推荐按`术语 → 术语自动决策 → 翻译 (→ 校对 → 应用校对 → 润色 → 应用润色) → 导出`的顺序操作。

在翻译页工具栏点击“临时翻译”，可粘贴文本进行翻译、取消请求或复制译文。输入和结果仅保留在当前页面，使用规则见[产品规格](MINIMAL.md#53-翻译)。

术语、术语自动决策、校对和润色可以根据实际需要跳过，不建议跳过术语。

### 4.1 术语

扫描源文并生成候选术语。扫描完成后可以：

- 搜索、编辑、移除和新增术语
- 设置首选译名、类别、说明和别名
- 对冲突候选进行人工裁决或建立术语组
- 导入或导出 JSON/CSV 术语表
- 扫描未完成时，发布已生成的候选

发布后，术语会用于翻译、校对和润色。重新扫描不会自动恢复已移除的术语，导入术语表也不会删除表中未包含的现有条目。

#### 实验：实体说明

选择术语，在“实体说明”面板编辑说明并选择引用片段。展开面板后加载实体详情。展开“说明片段与引用”可查看原文、停用或恢复片段。手工补充内容时，可选择“设为无来源人工说明”。

术语自动决策草案会展示说明的前后变化，同一实体的名称、关系和说明一起接受或拒绝。

在“自动决策”页面点击“仅实体说明”，可重新生成全部实体说明，并保留名称、译名、分组及人工待办。之后可在草案中审阅、应用或撤销。

- “待更新”时仍使用当前说明，可重新聚合，或在拒绝更新后选择“保持当前说明”
- “过期”时需重新聚合或人工修复，才能继续处理命中该实体的翻译、校对和润色
- 强制重扫会替换相关说明片段，运行前需确认替换范围

在“术语”设置中，可分别选择有来源与无来源说明的注入方式。默认全部注入。“优先前文”会优先使用前文或当前范围说明，策略细节见[产品规格](MINIMAL.md#实体说明实验)。

在“自动术语决策”设置中，可选择额外生成无来源总结，或仅生成无来源总结。更改后重新运行术语自动决策，并审阅、应用草案。

在全局或项目 Prompt 设置中选择“实体说明”，可编辑提示词。

JSON/CSV 中的说明不包含来源引用。需要保留完整引用时，请备份项目。

#### 实验：术语扫描组合输出

在术语页的运行对话框中，可勾选“同时生成粗翻（实验）”和“同时生成概括（实验）”，两者可以同时开启，使用术语扫描模型。

- 粗翻结果可在翻译页查看、编辑和导出。完成术语自动决策并应用后，可通过校对修正初稿中的术语
- 概括范围在内容概括页勾选，不影响术语扫描范围
- 已有译文默认保留。强制重做前，请核对对话框中将替换的译文和概括范围

组合提示词可在 Prompt 页的术语分组中预览，对应中段分别在术语、翻译和片段概括中编辑。

#### 实验：术语自动决策

发布术语后，运行术语自动决策，生成名称、分组和说明的审核草案。启动前可核对模型和处理数量。用量估算默认关闭，可在“设置 → 全局设置 → 共享与认证 → 任务并发”中开启“启动前估算请求数和 Tokens”。开启后弹窗会显示估算值，失败时可查看原因并继续启动。

开启“自动终审”会增加请求，实际用量可能高于估算。

草案仍有待审项时，可点击“终审草案待审项”单独启动终审。完成后检查更新后的建议并手动应用。草案的类型筛选选择“人工待办”，或点击待办数量，可直接查看这些项目的原因和证据。

在草案中搜索、筛选并审核建议。展开“查看建议详情”可查看完整内容，展开引用可查看来源。组合关系建议需整体接受或拒绝，有争议的内容会标为待审。

确认“应用”后才会修改术语库。已有人工编辑的名称等字段受保护，说明仍可提出更新。已有草案可丢弃或替换。生成失败时可查看已保存建议和待办，或续用任务。

应用后可撤销最近一次修改，后续编辑术语或发布扫描结果会使该撤销点失效。

#### 实验：内容概括

在术语页打开“更多”→“内容概括 · 实验”。

1. 勾选“参与概括”的范围，点击“扫描术语并补摘要”
2. 在“片段概括”中查看结果，点击“聚合完整概括”生成完整结果
3. 点击“查看来源”核对原文，通过“导出 Markdown”导出所选完整概括及引用

参与范围会保存在项目中。搜索后的全选或取消全选只影响筛选结果，Markdown 导出范围独立选择。

中断后再次启动可补齐未完成内容。结果显示“过期”或“无法验证”时，请重新生成。旧完整概括仍可查看和导出。

“完全清空”会删除全部概括结果和进度，保留参与范围选择。项目有任务运行时不能清空。

### 4.2 翻译

进入“翻译”，选择运行范围并启动任务。已完成内容默认复用，失败和未完成内容可以继续处理。

启动前请核对运行对话框中的 Preset 和模型。设置变化后，界面会提示选择复用已有结果或重做所选范围。

如需参考前文概括，可在项目设置中启用。翻译跨边界合并时不使用概括。

翻译页面支持逐段查看和编辑，人工保存后即成为当前译文。原文下方可展开“本段命中术语”，查看推荐译名、说明和别名，点击条目可跳转到术语库。

#### 翻译校验

在设置的“翻译校验”中选择校验器：

- 建议级术语校验（`preferred_term_usage`）：译文未采用推荐译名时尝试修复，修复后仍未采用则保留译文并显示警告
- Segment 错位校验（实验）：抽查原文与译文的对应关系，确认错位时整批重译

需要语义复核时，先在全局设置中配置 Decision Preset，再在校验器中选择它。LLM 协议使用已有 LLM Preset。HTTP 协议需填写完整请求 URL、模型和凭据，以及所需的代理和上下文容量。

校验器可分别设置置信度门槛。术语复核还可选择携带的上文段数。“不确定警告阈值”设为 0 可关闭不确定警告。

人工保存译文时会显示校验结果，需要自行修改。

#### 重复译文一致性审查

翻译完成后，从翻译页列表操作区进入“重复译文一致性审查”，检查相同原文的不同译法。

1. 点击“生成草案”，审查整个项目
2. 核对模型与预计用量。默认过滤仅标点不同的译文，可关闭过滤。开启“使用 Decision 初筛”前需配置项目的 Decision Preset；可选“自动终审待审项”，使用更大原文上下文完成判断
3. 对照原文上下文审核建议，可改选组内已有译文、拒绝统一子组，或通过“应用此处”选择修改位置。初筛跳过的组可启用“手动统一”
4. 点击“应用”并确认，更新选中位置的译文，其中也可能包含人工编辑过的译文

文件筛选用于浏览包含所选文件的重复组，组内保留全部文件的出现。应用按已接受建议及“应用此处”的选择执行。

上下文比较使用重复审查专用 Preset，未单独选择时沿用全局 Preset，温度为 0.1。前置筛选继续使用项目的 Decision Preset。相关提示词可在 Prompt 页的“重复译文上下文比较”和“重复译文初筛”中编辑。

失败或取消后，可在启动弹窗中续用任务。终审失败会提供首轮及已完成终审的建议，可人工应用、丢弃或续用终审；替换生成时，这份新草案会替换旧草案。取消或首轮比较失败时保留旧草案。审核期间相关原文或译文变化时，需要重新生成草案。

“撤销最近一次应用”可恢复本次修改的译文。受影响片段后来被修改时，无法撤销。后续校对或润色仍可能改变措辞。

### 4.3 校对与应用

“校对”以当前翻译为基础生成建议。可逐条审阅并应用，也可批量应用。应用后建议才会成为校对结果。

筛选中的“待处理”表示已有译文但尚无校对结果，“缺少基准”表示尚无可供校对的译文。

### 4.4 润色与应用

润色同样需要审核并应用建议。若希望基于校对结果润色，请先应用校对建议，再启动润色。

### 4.5 Web 连续运行

在项目概览点击“连续运行”，选择起点和后续阶段，核对每一步的模型、范围和已有结果处理方式。存在未完成任务时，选择“续用”或“结束并新建”。

- 可从术语、翻译或校对开始。从校对开始需要已有完整翻译
- 从术语继续到标准翻译时，必须包含术语自动决策，并授权应用决策结果
- 从术语开始可同时生成粗翻或概括。开启粗翻会跳过标准翻译，后续校对和润色使用粗翻结果。开启概括后还可选择“生成完整概括”
- 包含翻译或联合粗翻时，可勾选重复译文一致性审查，勾选后才加入连续运行。自动终审与自动应用分别选择，均默认关闭
- 校对后继续润色时，会先自动应用校对建议。润色建议仍需人工应用

术语自动决策或一致性审查作为最后一步时，可选择是否自动应用。一致性审查有待审项或未经授权应用建议时，保留草案并停止后续阶段，需审核后再启动剩余步骤。

任何步骤失败或取消都会停止后续步骤，已完成结果保留。任务面板会显示各步骤状态和当前阶段进度。

### 4.6 任务取消与恢复

任务取消、网络失败或应用重启后，已成功的结果仍然保留。再次运行时，可以继续未完成内容。

同一项目一次只能运行一个写入任务。运行期间可切换项目和查看结果，修改操作需等待任务结束或取消。

刷新或重新打开页面后，可继续查看仍在运行的任务。顶部“任务 N”面板列出当前服务中的活动任务，可打开对应项目或逐项取消。重启服务后，需要重新启动未完成任务。

### 4.7 诊断

遇到请求失败或结果异常时，打开“诊断”：

- 实时请求面板可切换 LLM / Decision，查看进度、用量、延迟及失败请求
- 请求详情可对照输入和模型结果，也可查看原始请求与响应
- “历史 Run”可按项目、阶段和状态筛选执行记录，查看摘要及已保存的诊断

需要保留完整请求和响应时，在运行前启用 Debug，相关注意事项见[日志与敏感内容](#日志与敏感内容)。

## 5. 导出

在“导出”中选择：

- 结果阶段：翻译、已应用校对或已应用润色。
- 输出格式：保留各文件原格式，或统一输出 TXT。
- 文件范围：全部文件或指定 File。
- 单语或双语对照。

TXT、EPUB 和 SRT 会按各自 Document Adapter 重建。EPUB 导出会翻译已纳入 Segment 的目录文本，
并保留导航链接、元数据、图片、CSS、字体和其他未翻译资源。

> [!IMPORTANT]
> 部分 Adapter（如 EPUB）要求项目设置目标语言标签，否则无法导出。

导出不会把多个 File 合并为一个文件，也不支持单独导出某个 Segment。Web 可以逐个下载
输出，也可以下载 zip。桌面壳还可以选择本机保存位置。

## 6. 支持的输入与限制

### TXT

支持 `.txt` 和 `.text`，可导入文件或文件夹。应用会检测常见编码，导出保留逻辑行和空行，但不保证保留输入编码及换行符。

### EPUB

支持 EPUB 2.0/3.0，正文和目录都可翻译，导出保留导航链接、图片、样式、字体等资源。需要重新导入旧项目时，应用会提示。

替换源文件时，可在替换对话框中调整导入和运行格式设置，确认前请核对受影响内容。

#### Ruby 注音格式

可在文件的运行格式设置中选择发送给模型的 Ruby 格式：

| 选项 | 示例 |
| -- | -- |
| `aozora` | `｜漢字《かんじ》` |
| `short_xml` | `<r><b>漢字</b><y>かんじ</y></r>` |
| `compact` | `⟦R:漢字\|Y:かんじ⟧` |
| `base_only` | `漢字`，移除注音 |

`short_xml` 和 `compact` 只改变模型输入，界面仍显示青空格式。不同模型处理这些格式的正确率可能不同，可按实际效果选择。

连续的逐字强调 Ruby 在界面中会合并显示，例如 `｜漢《·》｜字《·》` 显示为 `｜漢字《·》`，导出时恢复逐字强调。

具体格式规则见 [EPUB Adapter 契约](ADAPTERS.md#epub-06)。

### SRT（独立插件）

支持标准 `.srt`，导出保留字幕序号和时间。双语输出在同一条字幕内按原文、译文排列。

不支持部分非标准时间格式和定位参数。HTML/ASS 样式标记可能被模型改变，导出后需检查。

当前不支持 PDF、DOCX、Markdown 或任意格式互转。

## 7. 数据、安全与局域网共享

### 数据位置

项目、用户设置、Preset、Adapter、凭据索引和日志默认存放在平台用户数据目录。macOS 默认路径是：

```text
~/Library/Application Support/another-llm-translator/
```

开发或特殊部署可以用 `ANOTHER_LLM_USER_ROOT` 指向其他用户数据根目录。请通过应用完成文件替换和项目操作，不要直接编辑项目目录中的数据库或输入副本。

在“设置 → 全局 → 存储”中更改数据位置，选择目标父目录，数据会迁移到其中的 `another-llm-translator/` 子目录。

- 桌面应用会自动迁移并重启服务
- 源码 Web 保存位置后，需停止服务，在源码目录使用同一 Python 环境执行 `python -m app.data_root apply-pending`，成功后再重启

设置 `ANOTHER_LLM_USER_ROOT` 后，数据位置由该环境变量管理，不能在应用内更改。

### 存储中心

打开“设置 → 全局 → 存储”，查看全局数据和各项目的占用，外部项目也会计入。

选择项目后，可清理 Debug 记录、导出文件和日志。按页面列出的运行记录、文件或日志组选择，核对可回收大小后确认删除。清理会永久删除所选内容。

“Run 快照”按占用显示运行记录，可展开查看配置、执行明细、草稿和断点。可逐项清理断点，也可点击“一键清理”。仍需保留的断点会显示原因。

项目详情中的“整理并压缩数据库”可清理无用的旧结果并压缩数据库。运行前会显示可清理数量。有任务运行时需等待结束，压缩失败时可按提示重试。

### 日志与敏感内容

普通日志不会保存完整 Prompt、源文、鉴权 Header 或未脱敏请求正文。Debug 模式会额外保存完整请求、响应和执行诊断，其中可能包含 Prompt 与源文。处理敏感材料时不要启用 Debug。

Python 插件拥有当前用户权限，请只安装可信插件。

### 局域网共享

Web 默认仅本机可访问。在设置中开启局域网共享并选择网络接口后，其他设备才能连接。

- 开启认证后，其他设备需先登录
- 不开启认证时，同网段客户端拥有完整项目操作和 LLM 请求权限，界面会持续警告。
- 仅适合可信局域网，使用 HTTP，不提供多账号或角色管理

> [!CAUTION]
> 不要把服务直接暴露到公网。

## 8. CLI（高级用法）

CLI 与 Web 共用项目存储、阶段执行、限速和恢复行为。以下示例在源码目录执行。
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

以下流程先应用校对，再基于校对结果润色：

```bash
python -m app.main init novel.txt --name novel
python -m app.main terminology novel
python -m app.main translate novel
python -m app.main proofread novel
python -m app.main apply novel --stage proofreading --all
python -m app.main polish novel
python -m app.main apply novel --stage polishing --all
python -m app.main export novel --stage polished --bilingual
```

`python -m app.main run-all novel` 可一次生成术语、翻译、校对和润色结果，但不会自动应用建议。需要基于校对结果润色时，请按上述步骤执行。

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

`files-replace` 替换指定文件，保留文件顺序和导出文件名。确认前请查看受影响内容及可保留的进度。`--dry-run` 只预览，交互终端会询问确认，脚本需使用 `--yes`。

替换时可用 `--adapter-option ADAPTER.OPTION=VALUE` 调整指定选项，其他选项沿用原值。

可以用 `files-options` 查看或修改 File 的运行格式设置：

```bash
python -m app.main files-options novel --file-id F0001
python -m app.main files-options novel --file-id F0001 \
  --adapter-option epub.ruby_mode=short_xml
python -m app.main files-options novel --document-adapter epub
python -m app.main files-options novel --document-adapter epub \
  --adapter-option epub.inline_format_mode=markers
```

`--file-id` 只作用于一个 File。`--document-adapter` 作用于项目内该 Adapter 的全部当前 File。
省略 `--adapter-option` 时只查看设置，指定它时只覆盖列出的选项，其他选项保持原值。
设置变化只影响之后新启动的任务。

替换文件前，需先结束项目中的运行任务，并发布或丢弃未发布的术语扫描候选。

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

- `--dry-run`：预览处理范围和用量估算，不执行任务。
- `--force`：重做所选范围内已完成的结果。
- `--reuse-mixed-fingerprints`：设置变化后仍复用已完成结果。
- `--resume-run`、`--decline-run`：继续未完成的运行，或结束该运行并新建。

以具体子命令的 `--help` 为准，例如 `python -m app.main translate --help`。

### 术语管理与自动决策

```bash
python -m app.main terms-export novel glossary.json
python -m app.main terms-export novel glossary.csv
python -m app.main terms-import novel glossary.json
python -m app.main terms-publish-partial novel
python -m app.main terms-decide novel --dry-run
# 仅重新生成实体说明
python -m app.main terms-decide novel --knowledge-only
python -m app.main terms-decide novel
python -m app.main terms-decide-show novel
python -m app.main terms-decide-apply novel --all --reject TDP-EXAMPLE
python -m app.main terms-decide-rollback novel --confirm
```

`terms-decide` 需单独运行，生成后通过 `terms-decide-show` 审核，再使用 `terms-decide-apply --all` 应用，可重复使用 `--reject` 排除建议。

- `--resume-run`：继续未完成的自动决策任务
- `--force`：结束未完成任务并从头重做
- `--replace-draft`：替换待审核草案

应用后的撤销限制见[术语自动决策](#实验术语自动决策)。

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
- [模块职责](MODULES.md)：代码模块职责和依赖方向。
- [开发指南](DEVELOPMENT.md)：环境、调试、测试和桌面打包。
- [产品路线图](ROADMAP.md)：尚未实现的方向、进入条件和暂缓原因。
