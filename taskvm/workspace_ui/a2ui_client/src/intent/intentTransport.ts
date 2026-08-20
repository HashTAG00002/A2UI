/**
 * intentTransport — the A6 free-text intent endpoint's CLIENT half.
 *
 * The frozen contract (agentAPP.6 owns the server half; mock-first here,
 * live wiring once the endpoint's parser is composed in):
 *
 *   POST /api/app/a2ui/intent    body: {"text": "<自由文本>"}
 *   → 200 {"ok":true,"kind":"local_patch|goal_patch|checkpoint|rollback",
 *          ...kind 载荷..., "rationale":...}
 *   → 200 {"ok":true,"kind":"clarify","question":"...","intent":{...}}
 *   → 4xx {"ok":false,"error":"..."}
 *
 * The server (a2ui_transport.apply_intent) answers with `{ok, kind,
 * result, intent}` for executable kinds and `{ok, kind:"clarify",
 * question, intent}` for a clarify — `intent` carrying the parsed
 * payload (updates / goal / checkpoint_label / question / rationale).
 */

/** The structured intent payload the server echoes (ParsedIntent.to_payload). */
export interface IntentPayload {
  kind: string;
  source?: string;
  /** local_patch: {semantic_key: literal} */
  updates?: Record<string, unknown>;
  /** goal_patch */
  goal?: string;
  constraints?: string[];
  scope?: string[];
  success_criteria?: string[];
  /** checkpoint / rollback */
  checkpoint_label?: string;
  /** clarify */
  question?: string;
  rationale?: string;
}

export type IntentKind =
  | 'local_patch'
  | 'goal_patch'
  | 'checkpoint'
  | 'rollback'
  | 'clarify';

export type IntentResponse =
  | {
      ok: true;
      kind: 'local_patch' | 'goal_patch' | 'checkpoint' | 'rollback';
      result?: unknown;
      intent: IntentPayload;
    }
  | { ok: true; kind: 'clarify'; question: string; intent: IntentPayload }
  | { ok: false; error: string };

export interface IntentTransport {
  postIntent(text: string): Promise<IntentResponse>;
}

/** The real transport: one POST, errors surfaced verbatim — no guessing. */
export const realIntentTransport: IntentTransport = {
  async postIntent(text: string): Promise<IntentResponse> {
    try {
      const res = await fetch('/api/app/a2ui/intent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      const body = await res
        .json()
        .catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
      return body as IntentResponse;
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
  },
};
