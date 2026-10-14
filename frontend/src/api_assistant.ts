// Planning assistant API: stateless chat; the browser keeps the conversation and sends it with each question.

export type ChatRole = 'user' | 'assistant';

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

export interface AssistantToolCall {
  name: string;
  args: Record<string, unknown>;
  ok: boolean;
  result_summary: string;
  result: unknown;
}

export type AssistantMethod = 'llm' | 'llm_repaired' | 'fallback' | 'disabled';

export interface AssistantReply {
  answer: string;
  tool_calls: AssistantToolCall[];
  validated: boolean;
  method: AssistantMethod;
  numbers_checked: number;
  unsupported: string[];
  rounds: number;
  llm_disabled: boolean;
  model: string | null;
  latency_ms: number;
}

export async function askAssistant(
  messages: ChatMessage[],
  signal?: AbortSignal,
  style: 'default' | 'simple' = 'default',
): Promise<AssistantReply> {
  const r = await fetch('/api/assistant', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, style }),
    signal,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${r.status}: ${msg}`);
  }
  return (await r.json()) as AssistantReply;
}
