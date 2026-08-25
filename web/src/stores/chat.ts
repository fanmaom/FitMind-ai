import { create } from "zustand";
import type { Card, ChatMessage } from "@/types/chat";

type State = {
  messages: ChatMessage[]; conversationId?: string; toolStatus?: string;
  setConversationId: (id: string) => void; addUser: (text: string) => void;
  startAssistant: (id: string) => void; appendText: (text: string) => void;
  addCard: (card: Card) => void; finish: (level: number) => void;
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
  setToolStatus: (toolStatus) => set({ toolStatus }),
  setMessages: (messages) => set({ messages }),
}));
