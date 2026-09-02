"use client";

import { useState } from "react";
import { Check, CornerUpLeft, Plus, Trash2, Undo2, X } from "lucide-react";
import { api } from "@/services/api";

export type ActionItem = {
  id: string;
  content: string;
  category: string;
  status: "pending" | "done" | "ignored";
  source_message_id: string | null;
  source_conversation_id: string | null;
  source_quote: string | null;
  created_at: string;
};

const categoryLabels: Record<string, string> = {
  training: "训练",
  nutrition: "饮食",
  recovery: "恢复",
  general: "其他",
};

export function ActionPanel({
  items,
  onChanged,
}: {
  items: ActionItem[];
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pending = items.filter((item) => item.status === "pending");
  const settled = items.filter((item) => item.status !== "pending");

  async function setStatus(id: string, status: ActionItem["status"]) {
    await api(`/action-items/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    onChanged();
  }

  async function remove(id: string) {
    await api(`/action-items/${id}`, { method: "DELETE" });
    onChanged();
  }

  async function add() {
    const content = draft.trim();
    if (!content || adding) return;
    setAdding(true);
    setError(null);
    try {
      await api("/action-items", {
        method: "POST",
        body: JSON.stringify({ content }),
      });
      setDraft("");
      onChanged();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "添加失败");
    } finally {
      setAdding(false);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <div className="flex gap-2">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void add();
            }}
            placeholder="自己加一条待办…"
            className="min-w-0 flex-1 rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-blue-500"
          />
          <button
            onClick={() => void add()}
            disabled={!draft.trim() || adding}
            className="flex items-center rounded-lg bg-blue-600 px-3 text-white disabled:bg-slate-200 disabled:text-slate-400"
            title="添加"
          >
            <Plus size={16} />
          </button>
        </div>
        {error && <p className="mt-1 text-xs text-red-500">{error}</p>}
      </div>

      {pending.length === 0 && (
        <div className="flex min-h-24 items-center justify-center text-center text-sm text-slate-400">
          还没有待办。聊聊训练或饮食，助理给出的建议会自动落到这里。
        </div>
      )}

      <div className="space-y-3">
        {pending.map((item) => (
          <Row
            key={item.id}
            item={item}
            onDone={() => void setStatus(item.id, "done")}
            onIgnore={() => void setStatus(item.id, "ignored")}
            onDelete={() => void remove(item.id)}
          />
        ))}
      </div>

      {settled.length > 0 && (
        <details className="rounded-lg border border-slate-200">
          <summary className="cursor-pointer px-3 py-2 text-xs text-slate-500">
            已处理 {settled.length} 条
          </summary>
          <div className="space-y-2 border-t border-slate-100 p-3">
            {settled.map((item) => (
              <div
                key={item.id}
                className="flex items-start justify-between gap-2 text-sm"
              >
                <span className="min-w-0 flex-1 text-slate-400 line-through">
                  {item.content}
                </span>
                <button
                  onClick={() => void setStatus(item.id, "pending")}
                  className="shrink-0 text-slate-400 hover:text-blue-600"
                  title="重新打开"
                >
                  <Undo2 size={14} />
                </button>
                <button
                  onClick={() => void remove(item.id)}
                  className="shrink-0 text-slate-400 hover:text-red-500"
                  title="删除"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function Row({
  item,
  onDone,
  onIgnore,
  onDelete,
}: {
  item: ActionItem;
  onDone: () => void;
  onIgnore: () => void;
  onDelete: () => void;
}) {
  const [showQuote, setShowQuote] = useState(false);

  // 没有会话导航，所以溯源分两级：目标消息在当前已加载的会话里就滚动高亮，
  // 不在（历史会话、或页面刚刷新过）就退回展示助理原话。
  function trace() {
    const node = item.source_message_id
      ? document.getElementById(`msg-${item.source_message_id}`)
      : null;
    if (node) {
      node.scrollIntoView({ behavior: "smooth", block: "center" });
      node.classList.add("ring-2", "ring-blue-400", "rounded-xl");
      window.setTimeout(
        () => node.classList.remove("ring-2", "ring-blue-400", "rounded-xl"),
        1800,
      );
      return;
    }
    setShowQuote((value) => !value);
  }

  return (
    <div className="rounded-lg border border-slate-200 p-3">
      <div className="flex items-start gap-2">
        <button
          onClick={onDone}
          className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border border-slate-300 text-transparent hover:border-blue-600 hover:text-blue-600"
          title="标记完成"
        >
          <Check size={12} />
        </button>
        <p className="min-w-0 flex-1 text-sm leading-6">{item.content}</p>
        <button
          onClick={onIgnore}
          className="shrink-0 text-slate-400 hover:text-amber-600"
          title="忽略（不再重复提示）"
        >
          <X size={15} />
        </button>
        <button
          onClick={onDelete}
          className="shrink-0 text-slate-400 hover:text-red-500"
          title="删除"
        >
          <Trash2 size={15} />
        </button>
      </div>

      <div className="mt-2 flex items-center gap-2 pl-6">
        <span className="rounded bg-blue-50 px-2 py-0.5 text-xs text-blue-700">
          {categoryLabels[item.category] ?? item.category}
        </span>
        <time className="text-xs text-slate-400">
          {new Date(item.created_at).toLocaleDateString()}
        </time>
        {(item.source_message_id || item.source_quote) && (
          <button
            onClick={trace}
            className="flex items-center gap-1 text-xs text-slate-400 hover:text-blue-600"
            title="来源"
          >
            <CornerUpLeft size={12} />
            来源
          </button>
        )}
      </div>

      {showQuote && item.source_quote && (
        <blockquote className="mt-2 border-l-2 border-slate-200 pl-2 text-xs text-slate-500">
          {item.source_quote}
        </blockquote>
      )}
      {showQuote && !item.source_quote && (
        <p className="mt-2 text-xs text-slate-400">这条没有留下原文摘录。</p>
      )}
    </div>
  );
}
