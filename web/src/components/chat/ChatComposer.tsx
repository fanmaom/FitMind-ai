"use client";
import { FormEvent, useEffect, useState } from "react";
import { Send } from "lucide-react";

export function ChatComposer({ onSend, disabled }: { onSend: (text: string) => Promise<void>; disabled?: boolean }) {
  const [text, setText] = useState("");
  useEffect(() => { const listener = (e: Event) => setText((e as CustomEvent<string>).detail); window.addEventListener("fitness-option", listener); return () => window.removeEventListener("fitness-option", listener); }, []);
  async function submit(e: FormEvent) { e.preventDefault(); const value = text.trim(); if (!value || disabled) return; setText(""); await onSend(value); }
  return <form onSubmit={submit} className="border-t border-slate-200 bg-white p-4"><div className="mx-auto flex max-w-4xl items-end gap-2 rounded-2xl border border-slate-300 bg-white p-2 shadow-sm focus-within:border-blue-500"><textarea value={text} onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) submit(e); }} placeholder="告诉我你的目标，或直接记录：卧推 80kg 5×5" rows={2} className="max-h-32 flex-1 resize-none border-0 bg-transparent px-2 py-1 outline-none"/><button disabled={disabled || !text.trim()} className="rounded-xl bg-blue-600 p-3 text-white disabled:opacity-40"><Send size={18}/></button></div></form>;
}
