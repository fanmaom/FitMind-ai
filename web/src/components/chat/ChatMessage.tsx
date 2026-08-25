import { AlertTriangle, Bot, User } from "lucide-react";
import { CardRenderer } from "@/components/cards/CardRenderer";
import type { ChatMessage as Message } from "@/types/chat";

export function ChatMessage({ message }: { message: Message }) {
  const mine = message.role === "user";
  return <div className={`flex gap-3 ${mine ? "flex-row-reverse" : ""}`}><div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full ${mine ? "bg-slate-800 text-white" : "bg-blue-600 text-white"}`}>{mine ? <User size={16}/> : <Bot size={16}/>}</div><div className={`max-w-[82%] ${mine ? "rounded-2xl rounded-tr-sm bg-slate-800 px-4 py-3 text-white" : "min-w-0 flex-1"}`}><div className={mine ? "" : "whitespace-pre-wrap leading-7"}>{message.text || (message.pending ? "正在思考…" : "")}</div>{message.cards.map((card, i) => <CardRenderer card={card} key={`${card.type}-${i}`} />)}{(message.degradedTo ?? 0) > 0 && <div className="mt-2 flex items-center gap-1 text-xs text-amber-600"><AlertTriangle size={13}/>本次回答启用了 {message.degradedTo} 级降级策略</div>}</div></div>;
}
