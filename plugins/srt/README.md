# Another LLM Translator SRT 插件

这是一个 SRT `Document Adapter` 目录插件。宿主从 `plugin.toml` 读取描述，并在启动时
加载同目录的 `plugin.py`；插件代码使用 `app.plugin_api` 的公开契约。

插件把每个字幕 cue 映射为一个 Segment，保留序号和时间行，并支持纯译文与
双语 SRT 导出。双语 cue 按“原文、换行、译文”排列。

插件只接受核心 SRT 结构：正整数序号、`HH:MM:SS,mmm --> HH:MM:SS,mmm`
时间行和非空正文。序号不要求连续，但必须唯一。缺序号、点号毫秒、时间行尾
定位参数和其他非核心变体会被拒绝。

cue 正文（包括 HTML/ASS 样式标记）原样提供给模型；插件不解析或保证模型
保留这些标记。输出中的空白分隔行会被拒绝，因为它会改变 SRT cue 边界。

官方桌面构建会在构建时装配此目录。用户可以将满足同一目录契约的插件解压到用户数据
目录的 `plugins/` 下；应用重启后发现。
