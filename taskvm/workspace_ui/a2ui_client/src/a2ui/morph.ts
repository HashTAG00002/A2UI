/**
 * morph — §20.1 progressive-plane state machine (PURE).
 *
 * Maps server progress signals onto the island's visible state. The
 * honesty rules (workplan §20.1, test-pinned in __tests__/transport.test.ts):
 *
 *  - every stage transition consumes a REAL server signal — no local
 *    timers, no fabricated skeletons, no guessed values;
 *  - T0/T1 show variable LABELS only (values stay pending marks);
 *  - once `live`, late/replayed t1/t2 hints never morph the plane back
 *    (the A2UI surface is authoritative from then on);
 *  - a new `goal` signal resets the whole plane honestly;
 *  - unknown stages are ignored — never guessed.
 */
import type {
  CompilePhase,
  VariableSkeleton,
  WorkflowNodeChip,
} from '../progressive/ProgressiveTaskPlane';
import type { TaskStatus } from '../governance/GovernanceShell';
import type { ProgressSignal } from './transport';

export interface IslandState {
  phase: CompilePhase;
  status: TaskStatus;
  goal: string;
  skeletons: VariableSkeleton[];
  nodes: WorkflowNodeChip[];
  streamError: string | null;
}

export const INITIAL_ISLAND_STATE: IslandState = {
  phase: 't0',
  status: 'compiling',
  goal: '',
  skeletons: [],
  nodes: [],
  streamError: null,
};

const KINDS = new Set(['step', 'verification', 'checkpoint', 'goal']);
const STATUSES = new Set(['waiting', 'executing', 'verified', 'failed']);

/** Collapse a raw progress-node onto the four known chip tones (shared
 * by the T2 plane and the A7 snake trajectory). Unknown kinds/statuses
 * degrade honestly to step/waiting — never a guessed tone. */
export function toChip(n: { label: string; kind: string; status: string }): WorkflowNodeChip {
  return {
    label: n.label,
    kind: (KINDS.has(n.kind) ? n.kind : 'step') as WorkflowNodeChip['kind'],
    status: (STATUSES.has(n.status) ? n.status : 'waiting') as WorkflowNodeChip['status'],
  };
}

export function reduceProgress(
  state: IslandState,
  ev: ProgressSignal,
): IslandState {
  switch (ev.stage) {
    case 'goal':
      // a new instruction resets the whole plane — the compile chain
      // restarts from T0 with the user's actual goal text
      return { ...INITIAL_ISLAND_STATE, goal: ev.goal ?? '' };
    case 't1':
      if (state.phase === 'live') return state;
      return {
        ...state,
        phase: 't1',
        skeletons: (ev.variables ?? []).map((v) => ({ label: v.label })),
      };
    case 't2':
      if (state.phase === 'live') return state;
      return { ...state, phase: 't2', nodes: (ev.nodes ?? []).map(toChip) };
    case 'ready':
      return {
        ...state,
        phase: 'live',
        status: state.status === 'compiling' ? 'ready' : state.status,
        streamError: null,
      };
    case 'goal_failed':
      return { ...state, status: 'failed', streamError: ev.error ?? '任务编排失败' };
    case 'a2ui_failed':
      // the goal itself is healthy (kernel + runtime + fixed shell); only
      // the dynamic surface failed to mint — shown as an inline error
      return {
        ...state,
        streamError: (ev.errors ?? []).join('；') || '动态任务面铸造失败',
      };
    default:
      return state;
  }
}

/** The A2UI messages themselves are the authoritative "surface exists"
 * evidence — the reconnect/replay path uses this instead of trusting a
 * progress hint (the small ring may have overflowed). */
export function withSurfacesLive(state: IslandState): IslandState {
  if (state.phase === 'live') return state;
  return {
    ...state,
    phase: 'live',
    status: state.status === 'compiling' ? 'ready' : state.status,
  };
}
