# 模块职责

后端按业务职责组织实现，依赖方向由入口指向具体模块：

- `app/execution.py` 负责 Scope、Chunk 规划、Run 生命周期、用量汇总和并发调度；`app/llm_client.py` 与 `app/llm_response.py` 负责模型通信及响应解析。
- `app/stage_runtime.py` 提供阶段共享运行逻辑；`app/stage_translation.py`、`app/stage_review.py`、`app/stage_terminology.py` 分别实现翻译、审校和术语阶段；`app/project_export.py` 负责项目导出。
- `app/term_library.py`、`app/term_exchange.py`、`app/term_matching.py` 负责术语库、交换格式和匹配；`app/term_decision.py` 保留裁决执行入口，规则、批次和草稿分别位于对应模块。
- `app/stages.py` 仅保留跨阶段入口、成功校验和完整检查。
- `app/web.py` 负责应用装配、鉴权、生命周期和静态文件；`app/web_*_routes.py` 按资源、项目、Segment、术语、任务和导出职责注册路由。

前端页面组件按页面和弹窗拆分：项目概览、导出、项目创建、替换、项目选择及输入队列各自位于 `web/src/components/` 对应文件；术语主页面保留列表状态，术语弹窗集中在 `TermDialogs.tsx`。
