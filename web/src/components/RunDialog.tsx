import { useState } from "react";
import type { RunDecision, TaskOptions } from "../types";

const stageLabels = {
  terminology: "术语",
  translation: "翻译",
  proofreading: "校对",
  polishing: "润色",
};

type ResultPolicy = "pending" | "reuse" | "force";

export function RunDialog({
  options,
  starting,
  onClose,
  onStart,
}: {
  options: TaskOptions;
  starting: boolean;
  onClose: () => void;
  onStart: (decision: RunDecision) => void;
}) {
  const [runAction, setRunAction] = useState<"resume" | "decline" | null>(
    options.running_run ? "resume" : null,
  );
  const [resultPolicy, setResultPolicy] = useState<ResultPolicy | null>(
    options.mismatched_fingerprint_completed ? null : "pending",
  );
  const resuming = runAction === "resume";
  const ready = resuming || resultPolicy !== null;

  function submit() {
    if (!ready) return;
    onStart({
      force: !resuming && resultPolicy === "force",
      reuse_mixed_fingerprints: !resuming && resultPolicy === "reuse",
      run_action: options.running_run ? runAction : null,
    });
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        aria-labelledby="run-dialog-title"
        aria-modal="true"
        className="modal run-dialog"
        role="dialog"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="page-heading">
          <div>
            <h2 id="run-dialog-title">运行{stageLabels[options.stage]}阶段</h2>
            <p>确认如何处理已有结果和未完成 Run。</p>
          </div>
        </div>
        <div className="run-counts">
          <span><strong>{options.selected}</strong>全部</span>
          <span><strong>{options.completed}</strong>已完成</span>
          <span><strong>{options.pending}</strong>待处理</span>
          <span><strong>{options.failed}</strong>失败</span>
        </div>

        {options.running_run && (
          <fieldset className="decision-group">
            <legend>发现未完成 Run</legend>
            <label className="radio-option decision-option">
              <input
                type="radio"
                checked={runAction === "resume"}
                onChange={() => setRunAction("resume")}
              />
              <span><strong>续用原 Run</strong><small>沿用原始范围，以当前配置和 Prompt 继续未完成项。</small></span>
            </label>
            <label className="radio-option decision-option">
              <input
                type="radio"
                checked={runAction === "decline"}
                onChange={() => setRunAction("decline")}
              />
              <span><strong>结束原 Run 并新建</strong><small>将 {options.running_run.run_id} 标记为已拒绝续用。</small></span>
            </label>
            <div className="run-details">
              <span>原范围：<code>{JSON.stringify(options.running_run.scope)}</code></span>
              <span>原端点：{options.running_run.previous.model} · {options.running_run.previous.endpoint}</span>
              <span>当前端点：{options.running_run.current.model} · {options.running_run.current.endpoint}</span>
            </div>
          </fieldset>
        )}

        {!resuming && (
          <fieldset className="decision-group">
            <legend>已有结果</legend>
            {options.mismatched_fingerprint_completed ? (
              <>
                <div className="warning-banner run-warning">
                  有 {options.mismatched_fingerprint_completed} 个已完成结果来自不同设置指纹，必须明确复用或重做。
                </div>
                <label className="radio-option decision-option">
                  <input
                    type="radio"
                    checked={resultPolicy === "reuse"}
                    onChange={() => setResultPolicy("reuse")}
                  />
                  <span><strong>复用已有结果</strong><small>保留不同设置的 completed，只处理待处理和失败项。</small></span>
                </label>
              </>
            ) : (
              <label className="radio-option decision-option">
                <input
                  type="radio"
                  checked={resultPolicy === "pending"}
                  onChange={() => setResultPolicy("pending")}
                />
                <span><strong>处理未完成项</strong><small>保留现有 completed，只处理待处理和失败项。</small></span>
              </label>
            )}
            <label className="radio-option decision-option">
              <input
                type="radio"
                checked={resultPolicy === "force"}
                onChange={() => setResultPolicy("force")}
              />
              <span>
                <strong>强制重做全部</strong>
                <small>
                  {options.stage === "terminology"
                    ? "创建完整替换扫描；新 revision 发布前继续使用当前术语库。"
                    : `重新处理全部 ${options.selected} 个非空 Segment。`}
                </small>
              </span>
            </label>
          </fieldset>
        )}

        <div className="modal-actions">
          <button className="quiet-button" disabled={starting} onClick={onClose}>取消</button>
          <button className="primary-button" disabled={!ready || starting} onClick={submit}>
            {starting ? "正在启动…" : "确认运行"}
          </button>
        </div>
      </div>
    </div>
  );
}
