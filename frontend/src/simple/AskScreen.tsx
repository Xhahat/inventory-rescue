import { useEffect, useRef, useState } from 'react';
import { askAssistant } from '../api_assistant';
import type { ChatMessage } from '../api_assistant';
import { Markdown } from '../pages/AssistantPage';
import { Hero, Problem, Working } from './parts';
import { tidyAnswer } from './plain';

const STORE_KEY = 'simple.ask.v1';
const MAX_SENT = 12;

const EXAMPLES = [
  'Which products will run out first?',
  'Which products might go to waste?',
  'What changes if I spend only 8 lakh?',
  'Explain my plan in simple words.',
];

function load(): ChatMessage[] {
  try {
    return JSON.parse(sessionStorage.getItem(STORE_KEY) ?? '[]') as ChatMessage[];
  } catch {
    return [];
  }
}

export default function AskScreen() {
  const [chat, setChat] = useState<ChatMessage[]>(load);
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    sessionStorage.setItem(STORE_KEY, JSON.stringify(chat));
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [chat, busy]);

  const ask = async (question: string) => {
    const text = question.trim();
    if (!text || busy) return;
    const next: ChatMessage[] = [...chat, { role: 'user', content: text }];
    setChat(next);
    setQ('');
    setBusy(true);
    setError(null);
    try {
      const r = await askAssistant(next.slice(-MAX_SENT), undefined, 'simple');
      const content =
        r.method === 'disabled'
          ? 'Sorry, questions are switched off right now. Please ask the person who set up this tool.'
          : r.method === 'fallback'
            ? "Sorry, I couldn't check my answer against your numbers, so I won't guess. Please try asking in a different way."
            : tidyAnswer(r.answer);
      setChat([...next, { role: 'assistant', content }]);
    } catch (e) {
      setError((e as Error).message);
      setChat(chat);
      setQ(text);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="s-screen">
      <Hero icon="chat" title="Ask a question" sub="Ask in your own words, like “What will run out first?”" />
      <div className="s-sheet">
      <Problem error={error} />

      {chat.length === 0 && (
        <>
          <p className="s-lead">Type your question, or tap one of these:</p>
          <div className="s-examples">
            {EXAMPLES.map((e) => (
              <button key={e} className="s-btn" onClick={() => ask(e)} disabled={busy}>
                {e}
              </button>
            ))}
          </div>
        </>
      )}

      <div className="s-chat">
        {chat.map((m, i) =>
          m.role === 'user' ? (
            <div key={i} className="s-q">
              {m.content}
            </div>
          ) : (
            <div key={i} className="s-a">
              <Markdown text={m.content} />
            </div>
          ),
        )}
        {busy && <Working text="Looking at your numbers… this takes a few seconds." />}
        <div ref={endRef} />
      </div>

      <div className="s-ask">
        <textarea
          className="s-textarea"
          rows={2}
          value={q}
          placeholder="Type your question here…"
          aria-label="Your question"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              ask(q);
            }
          }}
        />
        <div className="s-row">
          <button className="s-btn s-primary" onClick={() => ask(q)} disabled={busy || !q.trim()}>
            Ask
          </button>
          {chat.length > 0 && (
            <button className="s-link" onClick={() => setChat([])} disabled={busy}>
              Start over
            </button>
          )}
        </div>
      </div>
      </div>
    </div>
  );
}
