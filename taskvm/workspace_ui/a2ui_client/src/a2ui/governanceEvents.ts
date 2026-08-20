/**
 * governanceEvents — the A7 motion layer's event vocabulary and PURE state
 * machine. The SSE contract (frozen with agentAPP.6):
 *
 *   event: governance
 *   data: {"type":"governance",
 *          "kind":"checkpoint_added|checkpoint_reached|rollback|pause|resume|
 *                  stop|node_verified|node_failed|final_pass|final_fail",
 *          "label":"...","rev":<int>,"ts":<ms>,"detail":{...}}
 *
 * Honesty rules (test-pinned):
 *  - the celebration gate is a CLOSED mapping: only `final_pass` fires the
 *    full celebration, only `checkpoint_reached` fires the small reward;
 *    every other kind — final_fail / node_failed / rollback / pause /
 *    resume / stop / checkpoint_added / node_verified — NEVER celebrates;
 *  - a rollback event drives the reverse playback to the user-visible
 *    checkpoint LABEL; an unknown label honestly disables playback (the
 *    state change still lands when the server's node statuses arrive);
 *  - pause freezes, resume unfreezes — no fabricated progress either way;
 *  - labels only — no ids, no deep links, nothing the user cannot see.
 */
export const GOVERNANCE_KINDS = [
  'checkpoint_added',
  'checkpoint_reached',
  'rollback',
  'pause',
  'resume',
  'stop',
  'node_verified',
  'node_failed',
  'final_pass',
  'final_fail',
] as const;

export type GovernanceKind = (typeof GOVERNANCE_KINDS)[number];

export interface GovernanceSignal {
  type: 'governance';
  kind: GovernanceKind;
  /** user-visible label only — internal ids never enter the island */
  label?: string;
  rev?: number;
  ts?: number;
  detail?: Record<string, unknown>;
}

export type CelebrationKind = 'none' | 'checkpoint_reward' | 'final_pass';

export interface CelebrationState {
  kind: CelebrationKind;
  /** increments per NEW celebration — components key the burst off this */
  nonce: number;
  /** what the celebration is about (checkpoint label / final verdict) */
  label: string;
}

export interface CheckpointChipState {
  label: string;
  reached: boolean;
}

export interface RollbackPlayback {
  active: boolean;
  /** user-visible target checkpoint label (never an id) */
  targetLabel: string | null;
  /** increments per NEW rollback — keys the reverse playback */
  nonce: number;
}

export interface GovernanceMotionState {
  checkpoints: CheckpointChipState[];
  paused: boolean;
  stopped: boolean;
  rollback: RollbackPlayback;
  celebration: CelebrationState;
  /** the honest failure marker (node_failed / final_fail label), if any */
  failureLabel: string | null;
}

export const INITIAL_GOVERNANCE_MOTION: GovernanceMotionState = {
  checkpoints: [],
  paused: false,
  stopped: false,
  rollback: { active: false, targetLabel: null, nonce: 0 },
  celebration: { kind: 'none', nonce: 0, label: '' },
  failureLabel: null,
};

/**
 * The CLOSED celebration gate (A7 acceptance: 终局 PASS 才 🎉；conflict /
 * verification failure 一律不庆祝). Every kind not listed returns `none`.
 */
export function celebrationFor(kind: GovernanceKind): CelebrationKind {
  if (kind === 'final_pass') return 'final_pass';
  if (kind === 'checkpoint_reached') return 'checkpoint_reward';
  return 'none';
}

function isGovernanceSignal(raw: unknown): raw is GovernanceSignal {
  if (typeof raw !== 'object' || raw === null) return false;
  const r = raw as Record<string, unknown>;
  return (
    r.type === 'governance' &&
    (GOVERNANCE_KINDS as readonly string[]).includes(String(r.kind))
  );
}

/**
 * Parse a raw SSE `governance` data frame into a GovernanceSignal.
 * Malformed frames are dropped (never guessed) — the same discipline as
 * the progress listener in transport.ts.
 */
export function parseGovernanceSignal(raw: unknown): GovernanceSignal | null {
  if (!isGovernanceSignal(raw)) return null;
  const r = raw as unknown as Record<string, unknown>;
  const out: GovernanceSignal = {
    type: 'governance',
    kind: r.kind as GovernanceKind,
  };  if (typeof r.label === 'string' && r.label) out.label = r.label;
  if (typeof r.rev === 'number') out.rev = r.rev;
  if (typeof r.ts === 'number') out.ts = r.ts;
  if (typeof r.detail === 'object' && r.detail !== null) {
    out.detail = r.detail as Record<string, unknown>;
  }
  return out;
}

export function reduceGovernance(
  state: GovernanceMotionState,
  ev: GovernanceSignal,
): GovernanceMotionState {
  const label = ev.label ?? '';
  switch (ev.kind) {
    case 'checkpoint_added': {
      if (!label) return state;
      if (state.checkpoints.some((c) => c.label === label)) return state;
      return {
        ...state,
        checkpoints: [...state.checkpoints, { label, reached: false }],
      };
    }
    case 'checkpoint_reached': {
      const nextCheckpoints = state.checkpoints.map((c) =>
        c.label === label ? { ...c, reached: true } : c,
      );
      const kind = celebrationFor('checkpoint_reached');
      return {
        ...state,
        checkpoints: nextCheckpoints,
        // the small reward fires only for a labeled checkpoint
        celebration: label
          ? { kind, nonce: state.celebration.nonce + 1, label }
          : state.celebration,
      };
    }
    case 'rollback': {
      // honest: without a resolvable user-visible label there is nothing
      // to play back to — the state change still lands when the server's
      // node statuses arrive
      const known = label !== '' && state.checkpoints.some((c) => c.label === label);
      const idx = known
        ? state.checkpoints.findIndex((c) => c.label === label)
        : -1;
      return {
        ...state,
        checkpoints:
          idx >= 0 ? state.checkpoints.slice(0, idx + 1) : state.checkpoints,
        rollback: {
          active: known,
          targetLabel: known ? label : null,
          nonce: state.rollback.nonce + 1,
        },
        // a rollback NEVER celebrates — and never bumps the nonce
        celebration: state.celebration,
      };
    }
    case 'pause':
      return { ...state, paused: true };
    case 'resume':
      return { ...state, paused: false };
    case 'stop':
      return { ...state, stopped: true, paused: false };
    case 'node_verified':
      // node truth itself lands in the snake model (by label)
      return state;
    case 'node_failed':
      // surfaced honestly; NEVER a celebration
      return { ...state, failureLabel: label || state.failureLabel };
    case 'final_pass':
      return {
        ...state,
        stopped: true,
        celebration: {
          kind: 'final_pass',
          nonce: state.celebration.nonce + 1,
          label,
        },
      };
    case 'final_fail':
      return {
        ...state,
        stopped: true,
        failureLabel: label || state.failureLabel,
        // negative gate: a fail verdict cancels any pending celebration
        celebration: { kind: 'none', nonce: state.celebration.nonce, label: '' },
      };
    default:
      return state;
  }
}
