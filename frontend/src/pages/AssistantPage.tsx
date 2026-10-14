import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { askAssistant } from '../api_assistant';
import type { AssistantReply, ChatMessage } from '../api_assistant';
import { Badge, Card, ErrorBox, Spinner } from '../ui';

interface Entry {
  role: 'user' | 'assistant';
  content: string;
  reply?: AssistantReply;
}

const STORE_KEY = 'assistant.chat.v1';
const MAX_SENT = 12;

const EXAMPLES = [
  'Why are we not expediting SKU-015?',
  'Protect hero SKUs, budget 8 lakh: what changes?',
  'What happens if SUP-04 slips another week?',
  'Which SKUs have expiry risk and what would a 35% markdown do?',
  'Compare spending 10 lakh vs 15 lakh.',
  'Draft the expedite request email to SUP-04 for the approved plan',
];

function loadEntries(): Entry[] {
  try {
    const raw = sessionStorage.getItem(STORE_KEY);
    return raw ? (JSON.parse(raw) as Entry[]) : [];
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------- minimal, safe markdown (no HTML injection)

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|\b(?:SKU-\d{3})\b)/g;

function inline(text: string, key: string): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    const k = `${key}-${i}`;
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4)
      return <strong key={k}>{inline(part.slice(2, -2), k)}</strong>;
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) return <code key={k}>{part.slice(1, -1)}</code>;
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) return <em key={k}>{part.slice(1, -1)}</em>;
    if (/^SKU-\d{3}$/.test(part))
      return (
        <a key={k} href={`#/sku/${part}`}>
          {part}
        </a>
      );
    return part;
  });
}

export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) blocks.push(<p key={`p${blocks.length}`}>{inline(para.join(' '), `p${blocks.length}`)}</p>);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const k = `l${blocks.length}`;
    const items = list.items.map((it, i) => <li key={i}>{inline(it, `${k}-${i}`)}</li>);
    blocks.push(list.ordered ? <ol key={k}>{items}</ol> : <ul key={k}>{items}</ul>);
    list = null;
  };
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const num = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const head = line.match(/^#{1,4}\s+(.*)$/);
    if (bullet || num) {
      flushPara();
      const ordered = !bullet;
      if (list && list.ordered !== ordered) flushList();
      if (!list) list = { ordered, items: [] };
      list.items.push((bullet ?? num)![1]);
    } else if (head) {
      flushPara();
      flushList();
      blocks.push(<h4 key={`h${blocks.length}`}>{inline(head[1], `h${blocks.length}`)}</h4>);
    } else if (!line.trim()) {
      flushPara();
      flushList();
    } else if (list && /^\s{2,}/.test(raw)) {
      list.items[list.items.length - 1] += ' ' + line.trim();
    } else {
      flushList();
      para.push(line.trim());
    }
  }
  flushPara();
  flushList();
  return <div className="md">{blocks}</div>;
}

// ---------------------------------------------------------------- answer metadata

function ValidationBadge({ reply }: { reply: AssistantReply }) {
  const n = reply.numbers_checked;
  switch (reply.method) {
    case 'llm':
      return (
        <Badge kind="ok" title="Every number, date and id in the answer was found in this conversation's tool results">
          ✓ Validated · {n} values checked
        </Badge>
      );
    case 'llm_repaired':
      return (
        <Badge kind="ok" title="The first draft quoted a value not found in the tool results; the model corrected it">
          ✓ Validated after 1 correction · {n} values checked
        </Badge>
      );
    case 'fallback':
      return (
        <Badge
          kind="warn"
          title={reply.unsupported.length ? `Unsupported values: ${reply.unsupported.join(', ')}` : undefined}
        >
          Fallback: deterministic summary of tool results
        </Badge>
      );
    default:
      return <Badge kind="bad">LLM disabled</Badge>;
  }
}

function ToolsUsed({ reply }: { reply: AssistantReply }) {
  if (!reply.tool_calls.length) return null;
  return (
    <details className="as-tools">
      <summary>
        Tools used ({reply.tool_calls.length}) · {reply.rounds} round{reply.rounds === 1 ? '' : 's'}
      </summary>
      <ol>
        {reply.tool_calls.map((c, i) => (
          <li key={i}>
            <div>
              <code className="as-call">
                {c.name}({Object.keys(c.args).length ? JSON.stringify(c.args) : ''})
              </code>{' '}
              {c.ok ? <Badge kind="ok">ok</Badge> : <Badge kind="bad">error</Badge>}
            </div>
            <div className="muted small">{c.result_summary}</div>
            <details>
              <summary className="small">Result JSON</summary>
              <pre className="as-json">{JSON.stringify(c.result, null, 2)}</pre>
            </details>
          </li>
        ))}
      </ol>
    </details>
  );
}

function AssistantBubble({ e }: { e: Entry }) {
  const r = e.reply;
  return (
    <div className={`as-msg as-assistant${r?.method === 'fallback' ? ' as-fallback' : ''}`}>
      {r?.method === 'disabled' ? (
        <div className="notice warn">{e.content}</div>
      ) : (
        <Markdown text={e.content} />
      )}
      {r && (
        <div className="as-meta">
          <ValidationBadge reply={r} />
          {r.model && <span className="muted small">{r.model}</span>}
          {r.latency_ms > 0 && <span className="muted small">{(r.latency_ms / 1000).toFixed(1)} s</span>}
        </div>
      )}
      {r && <ToolsUsed reply={r} />}
    </div>
  );
}

// ---------------------------------------------------------------- page

export default function AssistantPage() {
  const [entries, setEntries] = useState<Entry[]>(loadEntries);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify(entries.slice(-40)));
    } catch {
      /* storage full or unavailable: the chat still works */
    }
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [entries, busy]);

  useEffect(() => () => abortRef.current?.abort(), []);

  async function send(text: string) {
    const q = text.trim();
    if (!q || busy) return;
    const next: Entry[] = [...entries, { role: 'user', content: q }];
    setEntries(next);
    setInput('');
    setError(null);
    setBusy(true);
    const history: ChatMessage[] = next
      .filter((e) => e.reply?.method !== 'disabled')
      .map((e) => ({ role: e.role, content: e.content }))
      .slice(-MAX_SENT);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const reply = await askAssistant(history, ctrl.signal);
      setEntries((cur) => [...cur, { role: 'assistant', content: reply.answer, reply }]);
    } catch (err) {
      if (!ctrl.signal.aborted) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (abortRef.current === ctrl) {
        abortRef.current = null;
        setBusy(false);
      }
    }
  }

  function clear() {
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
    setEntries([]);
    setError(null);
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Planning assistant</h1>
          <p className="muted">
            Ask about the plan in plain language. The model answers by calling read-only tools that wrap the planner and
            simulator; every number in an answer is checked against those tool results. Nothing is saved or sent.
          </p>
        </div>
        <button onClick={clear} disabled={!entries.length && !busy}>
          Clear chat
        </button>
      </div>

      <Card>
        <div className="as-list">
          {!entries.length && (
            <div className="muted">
              <p>Try one of these:</p>
            </div>
          )}
          {entries.map((e, i) =>
            e.role === 'user' ? (
              <div key={i} className="as-msg as-user">
                {e.content}
              </div>
            ) : (
              <AssistantBubble key={i} e={e} />
            ),
          )}
          {busy && (
            <div className="as-msg as-assistant muted">
              <Spinner label="Calling planner tools…" />
            </div>
          )}
          <div ref={endRef} />
        </div>

        <div className="as-examples">
          {EXAMPLES.map((q) => (
            <button key={q} className="small ghost" disabled={busy} onClick={() => send(q)}>
              {q}
            </button>
          ))}
        </div>

        <ErrorBox error={error} />

        <form
          className="as-input"
          onSubmit={(ev) => {
            ev.preventDefault();
            send(input);
          }}
        >
          <textarea
            rows={2}
            value={input}
            maxLength={4000}
            placeholder="Ask about the plan, a SKU, a supplier, a budget or a what-if… (Enter to send, Shift+Enter for a new line)"
            onChange={(ev) => setInput(ev.target.value)}
            onKeyDown={(ev) => {
              if (ev.key === 'Enter' && !ev.shiftKey) {
                ev.preventDefault();
                send(input);
              }
            }}
          />
          <button type="submit" className="primary" disabled={busy || !input.trim()}>
            Ask
          </button>
        </form>
      </Card>
    </div>
  );
}
