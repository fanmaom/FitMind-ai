export type Card = { type: string; payload: Record<string, any> };
export type ChatMessage = {
  id: string; role: "user" | "assistant"; text: string; cards: Card[];
  pending?: boolean; degradedTo?: number; error?: string; interrupted?: boolean;
};
export type SSEEventName = "message_start" | "text_delta" | "tool_start" | "tool_result" | "card" | "message_done" | "interrupted" | "error";
