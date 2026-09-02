export type Card = { type: string; payload: Record<string, any> };
// 截断的两种成因，给用户的说法和该采取的动作都不同：
//   output —— 这句话没说完（撞上模型单次输出上限），让他说"继续"
//   turns  —— 这件事没做完（Agent 轮次用尽），让他把问题拆小
export type TruncationKind = "output" | "turns";
export type ChatMessage = {
  id: string; role: "user" | "assistant"; text: string; cards: Card[];
  pending?: boolean; degradedTo?: number; error?: string; interrupted?: boolean;
  truncated?: TruncationKind[];
};
export type SSEEventName = "message_start" | "text_delta" | "tool_start" | "tool_result" | "card" | "message_done" | "interrupted" | "error";
