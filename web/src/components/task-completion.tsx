"use client";

import { Badge } from "@/components/ui/badge";
import type { Task } from "@/lib/types";

export function TaskCompletionBadge({ task }: { task: Task }) {
  const phase = task.completion?.phase;
  if (task.paused || task.status !== "running" || (phase !== "draining" && phase !== "reviewing")) return null;
  return <Badge variant="outline">{phase === "draining" ? "保存收尾结果" : "最终复核"}</Badge>;
}

export function TaskCompletionPanel({ task }: { task: Task }) {
  const completion = task.completion;
  if (!completion || completion.phase === "idle") return null;
  const active =
    task.status === "running" && !task.paused && (completion.phase === "draining" || completion.phase === "reviewing");
  let title = "最近一次收官记录";
  if (active) title = completion.phase === "draining" ? "正在保存 Worker 已有结果" : "正在最终复核";
  return (
    <details className="rounded-md border bg-muted/30 px-3 py-2 text-sm" open={active}>
      <summary className="cursor-pointer font-medium">
        {title}
        <span className="ml-2 font-normal text-muted-foreground">#{completion.attempt_id}</span>
      </summary>
      <div className="mt-2 space-y-2">
        <p className="whitespace-pre-wrap break-words">{completion.reason}</p>
        {active && (
          <p className="text-muted-foreground text-xs">
            本阶段不再发起新探测。期限：{new Date(completion.deadline_at).toLocaleString()}；仍可暂停任务。
          </p>
        )}
        {completion.review && <p className="whitespace-pre-wrap break-words">{completion.review}</p>}
        {!!completion.remaining_work?.length && (
          <>
            <p className="font-medium">收官时剩余检查及处置依据</p>
            <p className="text-muted-foreground text-xs">停止或中断不代表检查已验证通过。</p>
            <ul className="list-inside list-disc space-y-1">
              {completion.remaining_work.map((work) => (
                <li key={work.intent_id} className="break-words">
                  #{work.intent_id} {work.summary}：{work.reason || "未形成完成结论"}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </details>
  );
}
