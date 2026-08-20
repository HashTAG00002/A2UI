/**
 * transport — the island's REAL connection to the A2UI server half
 * (`taskvm/workspace_ui/a2ui_transport.py`).
 *
 * One EventSource carries three streams:
 *  - default (unnamed) SSE events `{"type":"a2ui","seq":N,"message":…}`
 *    — ordered A2UI protocol messages, resumable via `?after=N`;
 *  - named `progress` events — the §20.1 progressive-plane signals
 *    (goal | t1 | t2 | ready | a2ui_failed | goal_failed);
 *  - named `governance` events — the A7 motion signals (frozen contract
 *    with agentAPP.6: checkpoint_added | checkpoint_reached | rollback |
 *    pause | resume | stop | node_verified | node_failed | final_pass |
 *    final_fail).
 *
 * Reconnect discipline: on error the client CLOSES and re-opens with its
 * OWN cursor (EventSource's built-in retry would replay from the
 * original `after` — correct but wasteful), with bounded backoff. The
 * ordered a2ui tail is authoritative; progress events are transient
 * morph hints (the small server ring replays them on reconnect).
 */
import type { A2uiMessage } from './protocol';
import { parseGovernanceSignal, type GovernanceSignal } from './governanceEvents';

export type A2uiConnectionState = 'connecting' | 'open' | 'reconnecting';

/** §20.1 progress payloads — screen-visible fields only. */
export interface ProgressSignal {
  stage: string;
  goal?: string;
  variables?: { label: string }[];
  nodes?: { label: string; kind: string; status: string }[];
  errors?: string[];
  error?: string;
  surfaceId?: string;
}

export interface A2uiConnectionOptions {
  /** Ordered protocol messages (feed them to MessageProcessor). */
  onMessages: (messages: A2uiMessage[]) => void;
  /** Transient progressive-plane signals. */
  onProgress: (event: ProgressSignal) => void;
  /** Governance motion signals (A7). Malformed frames are dropped. */
  onGovernance?: (event: GovernanceSignal) => void;
  onConnectionChange?: (state: A2uiConnectionState) => void;
}

export interface A2uiConnection {
  close: () => void;
}

export function connectA2ui(opts: A2uiConnectionOptions): A2uiConnection {
  let lastSeq = 0;
  let closed = false;
  let es: EventSource | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let backoffMs = 1000;

  const open = () => {
    if (closed) return;
    es = new EventSource(`/api/app/a2ui/sse?after=${lastSeq}`);
    es.onopen = () => {
      backoffMs = 1000;
      opts.onConnectionChange?.('open');
    };
    es.onmessage = (ev: MessageEvent) => {
      try {
        const env = JSON.parse(ev.data as string);
        if (env && env.type === 'a2ui' && env.message) {
          if (typeof env.seq === 'number') lastSeq = env.seq;
          opts.onMessages([env.message as A2uiMessage]);
        }
      } catch {
        // a malformed frame is dropped; the `after` tail self-heals on
        // the next reconnect — never a guessed message
      }
    };
    es.addEventListener('progress', (ev) => {
      try {
        opts.onProgress(JSON.parse((ev as MessageEvent).data as string));
      } catch {
        // observability hint only — a bad frame is ignored
      }
    });
    es.addEventListener('governance', (ev) => {
      try {
        const signal = parseGovernanceSignal(
          JSON.parse((ev as MessageEvent).data as string),
        );
        // malformed frames (wrong type / unknown kind) are dropped —
        // never a guessed governance verdict
        if (signal !== null) opts.onGovernance?.(signal);
      } catch {
        // same discipline: a bad frame is ignored
      }
    });
    es.onerror = () => {
      es?.close();
      es = null;
      if (closed) return;
      opts.onConnectionChange?.('reconnecting');
      retryTimer = setTimeout(open, backoffMs);
      backoffMs = Math.min(backoffMs * 2, 8000);
    };
  };
  open();

  return {
    close: () => {
      closed = true;
      es?.close();
      es = null;
      if (retryTimer !== null) clearTimeout(retryTimer);
    },
  };
}

// ── the action write path (the ONLY one) ───────────────────────────────────

export interface ActionResult {
  ok: boolean;
  error?: string;
  result?: unknown;
}

/** POST the renderer action to the transport's action route; the server
 * re-validates against the same policy ground truth and lands ONE
 * governance local_patch. Errors are surfaced verbatim — no guessing. */
export async function postSurfaceAction(
  name: string,
  context: Record<string, unknown>,
): Promise<ActionResult> {
  try {
    const res = await fetch('/api/app/a2ui/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, context }),
    });
    const body = await res
      .json()
      .catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
    return body as ActionResult;
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

// ── refresh recovery (the fixed APP shell's public status route) ───────────

export interface AppStatusGoal {
  goal_id: string;
  goal: string;
  status: 'bootstrapping' | 'ready' | 'failed';
  error?: string;
}

export interface AppStatus {
  ok: boolean;
  goals?: AppStatusGoal[];
}

export async function fetchAppStatus(): Promise<AppStatus | null> {
  try {
    const res = await fetch('/api/app/status');
    return (await res.json()) as AppStatus;
  } catch {
    return null;
  }
}
