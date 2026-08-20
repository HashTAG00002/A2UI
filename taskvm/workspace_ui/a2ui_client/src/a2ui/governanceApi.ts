/**
 * governanceApi — the island's OPTIMISTIC first-response write path
 * (A9.1).
 *
 * The shell's governance buttons POST here (single-sid proxy on the
 * server: ``POST /api/app/governance/<command>``) — the SAME
 * driver/governance semantics as the frozen projection routes, but the
 * island never has to know the session id (GUI-only, repo contract §3).
 *
 * Contract (test-pinned):
 *  - ZERO model calls: the endpoint is a local driver/governance-port
 *    round trip (single-digit ms server-side);
 *  - the caller shows the optimistic receipt IMMEDIATELY (<100ms) and
 *    rolls back on `{ok:false}` — this module never swallows failures;
 *  - performance marks are stamped by the caller (click → ack), this
 *    module only carries the request.
 */
export type GovCommand =
  | 'start'
  | 'pause'
  | 'resume'
  | 'stop'
  | 'checkpoint'
  | 'rollback';

export interface GovResult {
  ok: boolean;
  action?: string;
  state?: string;
  error?: string;
}

export const GOV_COMMANDS: readonly GovCommand[] = [
  'start',
  'pause',
  'resume',
  'stop',
  'checkpoint',
  'rollback',
];

/** POST one governance command through the island's proxy route.
 * `label` is the user-visible checkpoint label (checkpoint/rollback).
 * Network/HTTP failures resolve to `{ok:false, error}` — never a throw,
 * so the optimistic-rollback path in the caller is total. */
export async function postGovernance(
  command: GovCommand,
  label?: string,
): Promise<GovResult> {
  try {
    const res = await fetch(`/api/app/governance/${command}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(label ? { label } : {}),
    });
    const body = (await res.json().catch(() => null)) as GovResult | null;
    if (body && typeof body.ok === 'boolean') return body;
    return { ok: false, error: `HTTP ${res.status}` };
  } catch (e) {
    return {
      ok: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}
