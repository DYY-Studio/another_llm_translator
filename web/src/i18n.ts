export type Language = "zh-CN" | "en";

const messages: Record<Language, Record<string, string>> = {
  "zh-CN": {
    "brand": "译工坊",
    "project.select": "选择项目",
    "project.create": "新建 / 打开",
    "nav.overview": "项目概览",
    "nav.diagnostics": "仪表盘",
    "nav.terminology": "术语",
    "nav.translation": "翻译",
    "nav.proofreading": "校对",
    "nav.polishing": "润色",
    "nav.export": "导出",
    "nav.settings": "设置",
    "run.queued": "等待中",
    "run.running": "运行中",
    "run.cancelling": "正在取消",
    "run.completed": "已完成",
    "run.failed": "失败",
    "run.cancelled": "已取消",
    "run.completedCount": "已完成 {completed} · 失败 {failed} · 待处理 {pending} / {total}",
    "run.tokensUnavailable": "精确 Tokens 不可用",
    "run.cancel": "取消任务",
    "language.switch": "English",
    "theme.system": "跟随系统",
    "theme.light": "浅色",
    "theme.dark": "深色",
    "diagnostics.title": "诊断仪表盘",
    "diagnostics.live": "每秒刷新",
    "diagnostics.noRun": "当前没有运行中的 LLM 任务",
    "diagnostics.currentRequests": "当前请求",
    "diagnostics.inputTokens": "输入 Tokens",
    "diagnostics.outputTokens": "输出 Tokens",
    "diagnostics.throughput": "总吞吐量",
    "diagnostics.concurrency": "并发数",
    "diagnostics.runTotal": "当前 Run 精确累计",
    "diagnostics.tokensPerSecond": "Tokens / 秒",
  },
  en: {
    "brand": "Translator",
    "project.select": "Select project",
    "project.create": "New / Open",
    "nav.overview": "Overview",
    "nav.diagnostics": "Dashboard",
    "nav.terminology": "Terms",
    "nav.translation": "Translation",
    "nav.proofreading": "Proofreading",
    "nav.polishing": "Polishing",
    "nav.export": "Export",
    "nav.settings": "Settings",
    "run.queued": "Queued",
    "run.running": "Running",
    "run.cancelling": "Cancelling",
    "run.completed": "Completed",
    "run.failed": "Failed",
    "run.cancelled": "Cancelled",
    "run.completedCount": "Done {completed} · Failed {failed} · Pending {pending} / {total}",
    "run.tokensUnavailable": "Exact token usage unavailable",
    "run.cancel": "Cancel task",
    "language.switch": "中文",
    "theme.system": "System",
    "theme.light": "Light",
    "theme.dark": "Dark",
    "diagnostics.title": "Diagnostics",
    "diagnostics.live": "Live · 1s",
    "diagnostics.noRun": "No LLM task is running",
    "diagnostics.currentRequests": "Current requests",
    "diagnostics.inputTokens": "Input tokens",
    "diagnostics.outputTokens": "Output tokens",
    "diagnostics.throughput": "Throughput",
    "diagnostics.concurrency": "Concurrency",
    "diagnostics.runTotal": "Exact total for current run",
    "diagnostics.tokensPerSecond": "Tokens / second",
  },
};

export function translate(
  key: string,
  language: Language,
  values: Record<string, string | number> = {},
): string {
  const template = messages[language][key] ?? messages["zh-CN"][key] ?? key;
  return template.replace(/\{(\w+)\}/g, (_, name: string) => String(values[name] ?? `{${name}}`));
}

export function detectLanguage(): Language {
  try {
    const value = window.localStorage.getItem("minimal-llm-translator.language.v1");
    if (value === "zh-CN" || value === "en") return value;
  } catch {
    // Browser storage is optional; use the navigator below.
  }
  return navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}
