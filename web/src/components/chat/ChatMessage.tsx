import { AlertTriangle, Bot, CircleStop, Scissors, User } from "lucide-react";
import { CardRenderer } from "@/components/cards/CardRenderer";
import type { ChatMessage as Message, TruncationKind } from "@/types/chat";

// 两种截断的成因不同，能做的事也不同——所以不合并成一句"回答不完整"。
// 用户看到半句话时最需要知道的是"我该怎么办"，而这两种情况的答案相反：
// 一种让他说"继续"，另一种让他把问题拆小（再说"继续"也还是会撞上轮次上限）。
const TRUNCATION_HINT: Record<TruncationKind, string> = {
  output: "这条回答达到单次输出上限，没有说完。回复「继续」可以接着往下写。",
  turns: "这次要做的步骤太多，没能全部完成。把问题拆成几步分别问会更可靠。",
};

export function ChatMessage({ message }: { message: Message }) {
  const mine = message.role === "user";
  return <div id={`msg-${message.id}`} className={`flex scroll-mt-6 gap-3 transition-shadow ${mine ? "flex-row-reverse" : ""}`}><div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full ${mine ? "bg-slate-800 text-white" : "bg-blue-600 text-white"}`}>{mine ? <User size={16}/> : <Bot size={16}/>}</div><div className={`max-w-[82%] ${mine ? "rounded-2xl rounded-tr-sm bg-slate-800 px-4 py-3 text-white" : "min-w-0 flex-1"}`}><div className={mine ? "" : "whitespace-pre-wrap leading-7"}>{message.text || (message.pending ? "正在思考…" : "")}</div>{message.cards.map((card, i) => <CardRenderer card={card} key={`${card.type}-${i}`} />)}{message.interrupted && <div className="mt-2 flex items-center gap-1 text-xs text-slate-400"><CircleStop size={13}/>已停止</div>}{message.truncated?.map(kind => <div className="mt-2 flex items-start gap-1 text-xs text-amber-700" key={kind}><Scissors className="mt-0.5 shrink-0" size={13}/><span>{TRUNCATION_HINT[kind]}</span></div>)}{(message.degradedTo ?? 0) > 0 && <div className="mt-2 flex items-center gap-1 text-xs text-amber-600"><AlertTriangle size={13}/>本次回答启用了 {message.degradedTo} 级降级策略</div>}</div></div>;
}
