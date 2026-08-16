# Another LLM Translator SRT 插件

这是一个独立发行的 SRT `Document Adapter`，通过
`another_llm_translator.plugins` entry point 注册到宿主。安装宿主后可显式
安装插件：

```bash
python -m pip install another-llm-translator another-llm-translator-srt
```

插件把每个字幕 cue 映射为一个 Segment，保留序号和时间行，并支持纯译文与
双语 SRT 导出。双语 cue 按“原文、换行、译文”排列。

首版只接受核心 SRT 结构：正整数序号、`HH:MM:SS,mmm --> HH:MM:SS,mmm`
时间行和非空正文。序号不要求连续，但必须唯一。缺序号、点号毫秒、时间行尾
定位参数和其他非核心变体会被拒绝。

cue 正文（包括 HTML/ASS 样式标记）原样提供给模型；插件不解析或保证模型
保留这些标记。输出中的空白分隔行会被拒绝，因为它会改变 SRT cue 边界。

官方桌面构建会在构建时装配此独立包。已发布桌面应用暂不提供运行时安装任意
第三方插件的机制。
