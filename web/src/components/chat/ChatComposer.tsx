"use client";
import { FormEvent, useState } from "react";
import { Send, Square } from "lucide-react";

export function ChatComposer({ onSend, onStop, sending }: { onSend: (text: string) => Promise<void>; onStop: () => void; sending?: boolean }) {
  const [text, setText] = useState("");
  // 原来这里监听一个 window 上的 "fitness-option" 事件，把冲突卡片选中的标题
  // 塞进输入框。已删除：卡片现在直接调 onPick 发送，不再经过输入框。
  //
  // 那套全局事件是"点了没反应"的根源——用户点完选项，页面主体毫无变化，
  // 注意力在卡片上根本不会注意到底部输入框多了几个字。而全局事件的两端互不
  // 知情，卡片不知道有没有人在听，坏了也没有任何报错。
  async function submit(e: FormEvent) { e.preventDefault(); const value = text.trim(); if (!value || sending) return; setText(""); await onSend(value); }
  return <form onSubmit={submit} className="border-t border-slate-200 bg-white p-4"><div className="mx-auto flex max-w-4xl items-end gap-2 rounded-2xl border border-slate-300 bg-white p-2 shadow-sm focus-within:border-blue-500"><textarea value={text} onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) submit(e); }} placeholder="告诉我你的目标，或直接记录：卧推 80kg 5×5" rows={2} className="max-h-32 flex-1 resize-none border-0 bg-transparent px-2 py-1 outline-none"/>
    {/* 生成中变停止键。刻意不复用 disabled：输入框空着时旧逻辑会把按钮置灰，
        而正在生成时输入框通常就是空的——那就点不动停止了。 */}
    {sending
      ? <button type="button" onClick={onStop} title="停止生成" aria-label="停止生成" className="rounded-xl bg-slate-700 p-3 text-white hover:bg-slate-800"><Square size={18} fill="currentColor"/></button>
      : <button type="submit" disabled={!text.trim()} title="发送" aria-label="发送" className="rounded-xl bg-blue-600 p-3 text-white disabled:opacity-40"><Send size={18}/></button>}
  </div></form>;
}
