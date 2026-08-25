import { API_BASE, getToken } from "@/services/api";
import type { SSEEventName } from "@/types/chat";

export async function streamChat(
  conversationId: string, text: string,
  onEvent: (event: SSEEventName, data: any) => void,
): Promise<void> {
  const response = await fetch(`${API_BASE}/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify({ text, client_message_id: crypto.randomUUID() }),
  });
  if (!response.ok || !response.body) throw new Error(await response.text());
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      let event = "";
      const dataLines: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (event && dataLines.length) onEvent(event as SSEEventName, JSON.parse(dataLines.join("\n")));
    }
  }
}
