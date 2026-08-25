"use client";
import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { api } from "@/services/api";
import { streamChat } from "@/hooks/useSSE";
import { useChatStore } from "@/stores/chat";
import { ChatComposer } from "./ChatComposer";
import { ChatMessage } from "./ChatMessage";

const toolLabels: Record<string, string> = { query_workout_history: "正在查询训练记录…", search_food: "正在查询食物营养…", plan_meals: "正在生成场景配餐…", plan_strength_cycle: "正在编排增力周期…", plan_cut_phase: "正在编排减脂周期…" };

export function ChatView({ onChanged }: { onChanged: () => void }) {
  const store = useChatStore(); const [sending, setSending] = useState(false); const end = useRef<HTMLDivElement>(null);
  useEffect(() => end.current?.scrollIntoView({ behavior: "smooth" }), [store.messages, store.toolStatus]);
  async function ensureConversation() { if (store.conversationId) return store.conversationId; const c = await api<{id:string}>("/conversations", { method: "POST" }); store.setConversationId(c.id); return c.id; }
  async function send(text: string) {
    setSending(true); store.addUser(text);
    try {
      const id = await ensureConversation();
      await streamChat(id, text, (event, data) => {
        if (event === "message_start") store.startAssistant(data.messageId);
        else if (event === "text_delta") store.appendText(data.text);
        else if (event === "tool_start") store.setToolStatus(toolLabels[data.name] ?? `正在调用 ${data.name}…`);
        else if (event === "tool_result") store.setToolStatus(undefined);
        else if (event === "card") store.addCard(data);
        else if (event === "message_done") { store.finish(data.degradedTo ?? 0); onChanged(); }
        else if (event === "error") { store.appendText(data.message); store.finish(data.degradation_level ?? 5); }
      });
    } catch (error) {
      store.appendText(`连接中断：${error instanceof Error ? error.message : "未知错误"}`);
      if (store.conversationId) { const rows = await api<any[]>(`/conversations/${store.conversationId}/messages`).catch(() => []); store.setMessages(rows.map(r => ({ id: r.id, role: r.role, text: r.content.text ?? "", cards: r.content.cards ?? [] }))); }
    } finally { setSending(false); }
  }
  return <section className="flex min-w-0 flex-1 flex-col bg-slate-50"><header className="border-b border-slate-200 bg-white px-6 py-4"><h1 className="text-lg font-bold">FitMind AI</h1><p className="text-xs text-slate-500">会记住你的训练目标、偏好与限制</p></header><div className="flex-1 overflow-y-auto p-5"><div className="mx-auto max-w-4xl space-y-6">{store.messages.length === 0 && <div className="py-20 text-center"><h2 className="text-2xl font-bold">今天想练什么？</h2><p className="mt-2 text-slate-500">我能记录训练、规划周期、计算营养，并根据你的场景配餐。</p></div>}{store.messages.map(m => <ChatMessage key={m.id} message={m}/>) }{store.toolStatus && <div className="flex items-center gap-2 text-sm text-blue-600"><LoaderCircle className="animate-spin" size={16}/>{store.toolStatus}</div>}<div ref={end}/></div></div><ChatComposer onSend={send} disabled={sending}/></section>;
}
