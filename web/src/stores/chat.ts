import { create } from "zustand";
import type { Card, ChatMessage } from "@/types/chat";

type State = {
  messages: ChatMessage[]; conversationId?: string; toolStatus?: string;
  setConversationId: (id: string) => void; addUser: (text: string) => void;
  startAssistant: (id: string) => void; appendText: (text: string) => void;
  addCard: (card: Card) => void; finish: (level: number) => void;
  interrupt: () => void;
  setToolStatus: (status?: string) => void; setMessages: (messages: ChatMessage[]) => void;
};
export const useChatStore = create<State>((set) => ({
  messages: [],
  setConversationId: (conversationId) => set({ conversationId }),
  addUser: (text) => set((s) => ({ messages: [...s.messages, { id: crypto.randomUUID(), role: "user", text, cards: [] }] })),
  startAssistant: (id) => set((s) => ({ messages: [...s.messages, { id, role: "assistant", text: "", cards: [], pending: true }] })),
  appendText: (text) => set((s) => ({ messages: s.messages.map((m, i) => i === s.messages.length - 1 ? { ...m, text: m.text + text } : m) })),
  addCard: (card) => set((s) => ({ messages: s.messages.map((m, i) => i === s.messages.length - 1 ? { ...m, cards: [...m.cards, card] } : m) })),
  finish: (degradedTo) => set((s) => ({ toolStatus: undefined, messages: s.messages.map((m, i) => i === s.messages.length - 1 ? { ...m, pending: false, degradedTo } : m) })),
  // 只标记助理消息。用户可能在 message_start 到达之前就点了停止，那时最后一条
  // 是他自己发的——不设这道闸，「已停止」角标会贴到用户自己的气泡上。
  interrupt: () => set((s) => {
    const last = s.messages[s.messages.length - 1];
    if (last?.role !== "assistant") return { toolStatus: undefined };
    return {
      toolStatus: undefined,
      messages: s.messages.map((m, i) => i === s.messages.length - 1 ? { ...m, pending: false, interrupted: true } : m),
    };
  }),
  setToolStatus: (toolStatus) => set({ toolStatus }),
  setMessages: (messages) => set({ messages }),
}));
