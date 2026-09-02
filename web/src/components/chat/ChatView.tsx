"use client";
import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { api } from "@/services/api";
import { streamChat } from "@/hooks/useSSE";
import { useChatStore } from "@/stores/chat";
import { ChatComposer } from "./ChatComposer";
import { ChatMessage } from "./ChatMessage";

// 状态行文案由后端给（tool_start 事件的 label）。前端不再维护工具名映射表：
// 那张表漏一个工具，用户就会看到"正在调用 calc_energy_baseline…"。
const FALLBACK_TOOL_STATUS = "正在处理…";

export function ChatView({ onChanged }: { onChanged: () => void }) {
  const store = useChatStore(); const [sending, setSending] = useState(false); const end = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => {
    // 新版 Chromium 的 scrollIntoView 可能返回 Promise。effect 若隐式返回它，
    // React 会把 Promise 当作清理函数，并在下一次流式更新时调用而崩溃。
    end.current?.scrollIntoView({ behavior: "smooth" });
  }, [store.messages, store.toolStatus]);
  async function ensureConversation() { if (store.conversationId) return store.conversationId; const c = await api<{id:string}>("/conversations", { method: "POST" }); store.setConversationId(c.id); return c.id; }
  async function stop() {
    const conv = store.conversationId;
    // 顺序不能反：必须先让后端插旗，再断连接。
    // 反过来的话后端在收到 interrupt 之前还在生成，那几个 token 会落进库里，
    // 变成"你没看见但模型看见了"——真中断当场退化成假中断。
    if (conv) await api(`/conversations/${conv}/interrupt`, { method: "POST" }).catch(() => undefined);
    abortRef.current?.abort();
    // 本地即时标记。abort 之后后端那个 interrupted 事件基本收不到了（连接已断），
    // 所以角标不能只依赖它。
    store.interrupt();
  }

  async function send(text: string) {
    const controller = new AbortController();
    abortRef.current = controller;
    setSending(true); store.addUser(text);
    try {
      const id = await ensureConversation();
      await streamChat(id, text, (event, data) => {
        if (event === "message_start") store.startAssistant(data.messageId);
        else if (event === "text_delta") store.appendText(data.text);
        else if (event === "tool_start") store.setToolStatus(data.label ? `正在${data.label}…` : FALLBACK_TOOL_STATUS);
        else if (event === "tool_result") store.setToolStatus(undefined);
        else if (event === "card") store.addCard(data);
        else if (event === "message_done") { store.finish(data.degradedTo ?? 0); onChanged(); }
        else if (event === "interrupted") { store.interrupt(); onChanged(); }
        else if (event === "error") { store.appendText(data.message); store.finish(data.degradation_level ?? 5); }
      }, controller.signal);
    } catch (error) {
      // 用户主动停止不是错误，必须先拦下来直接 return。
      // 下面那段重新拉取会用库里的文本覆盖 store，把屏幕上刚停住的半句冲掉——
      // abort 的效果会当场自我撤销。
      if (error instanceof DOMException && error.name === "AbortError") return;
      store.appendText(`连接中断：${error instanceof Error ? error.message : "未知错误"}`);
      if (store.conversationId) { const rows = await api<any[]>(`/conversations/${store.conversationId}/messages`).catch(() => []); store.setMessages(rows.map(r => ({ id: r.id, role: r.role, text: r.content.text ?? "", cards: r.content.cards ?? [] }))); }
    } finally { setSending(false); abortRef.current = null; }
  }
  return <section className="flex min-w-0 flex-1 flex-col bg-slate-50"><header className="border-b border-slate-200 bg-white px-6 py-4"><h1 className="text-lg font-bold">FitMind AI</h1><p className="text-xs text-slate-500">会记住你的训练目标、偏好与限制</p></header><div className="flex-1 overflow-y-auto p-5"><div className="mx-auto max-w-4xl space-y-6">{store.messages.length === 0 && <div className="py-20 text-center"><h2 className="text-2xl font-bold">今天想练什么？</h2><p className="mt-2 text-slate-500">我能记录训练、规划周期、计算营养，并根据你的场景配餐。</p></div>}{store.messages.map(m => <ChatMessage key={m.id} message={m}/>) }{store.toolStatus && <div className="flex items-center gap-2 text-sm text-blue-600"><LoaderCircle className="animate-spin" size={16}/>{store.toolStatus}</div>}<div ref={end}/></div></div><ChatComposer onSend={send} onStop={stop} sending={sending}/></section>;
}
