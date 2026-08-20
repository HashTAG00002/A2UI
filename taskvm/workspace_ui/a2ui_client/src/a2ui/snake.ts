/**
 * snake — the PURE verified-progress model behind the A7 snake trajectory
 * (workplan MASTER_HANDOVER L167: "verified snake progress 只跨 verified
 * milestone").
 *
 * Honesty rules (test-pinned):
 *  - the snake body only ever lies over VERIFIED nodes: a milestone is
 *    crossed iff the node is verified AND every node before it is too —
 *    a snake cannot skip, the contiguous verified prefix IS the body
 *    (a verified node behind a waiting sibling still shows its own ●
 *    chip state, but the trajectory never jumps to it);
 *  - an EXECUTING node shows the head poking into the segment toward it
 *    — never past its milestone ("探出头但不越界"): the poke is capped at
 *    half a segment; WAITING / FAILED nodes get no poke at all;
 *  - node truth arrives from two honest sources: progress `t2` node lists
 *    (the kernel's DAG view) and governance `node_verified` /
 *    `node_failed` events, matched by the user-visible LABEL;
 *  - the rollback playback targets the node whose label matches the
 *    rollback event — an unknown label disables playback honestly.
 */
import type {
  NodeStatusChip,
  WorkflowNodeChip,
} from '../progressive/ProgressiveTaskPlane';
import type { GovernanceSignal } from './governanceEvents';

export interface SnakeModel {
  /** the nodes the trajectory visits, in order */
  nodes: WorkflowNodeChip[];
  /** how many milestones the snake has crossed (contiguous verified) */
  crossed: number;
  /** index of the EXECUTING node the head pokes toward, or null */
  pokeIndex: number | null;
  /** index of the first failed node in the uncrossed remainder */
  failedIndex: number | null;
}

/** The poke never crosses the next milestone — half a segment at most. */
export const POKE_MAX = 0.5;

export function snakeModel(nodes: WorkflowNodeChip[]): SnakeModel {
  let crossed = 0;
  while (crossed < nodes.length && nodes[crossed].status === 'verified') {
    crossed += 1;
  }
  const ahead = nodes[crossed];
  const pokeIndex =
    ahead !== undefined && ahead.status === 'executing' ? crossed : null;
  let failedIndex: number | null = null;
  for (let i = crossed; i < nodes.length && failedIndex === null; i += 1) {
    if (nodes[i].status === 'failed') failedIndex = i;
  }
  return { nodes, crossed, pokeIndex, failedIndex };
}

/**
 * The head position along the trajectory, in milestone units:
 * `crossed + poke`. Guaranteed to stay strictly below the next
 * milestone (crossed + 1) while poking, and exactly on the last
 * milestone when everything before it is verified.
 */
export function snakeHeadUnits(model: SnakeModel): number {
  if (model.nodes.length === 0) return 0;
  const poke = model.pokeIndex === null ? 0 : POKE_MAX;
  return Math.min(model.crossed + poke, model.nodes.length - 1);
}

/**
 * Merge a governance node event into a node list BY LABEL (the only
 * identity the GUI ever sees). Unknown labels change nothing — never a
 * guess. Returns the same array reference when nothing matched.
 */
export function applyGovernanceToNodes(
  nodes: WorkflowNodeChip[],
  ev: GovernanceSignal,
): WorkflowNodeChip[] {
  if (ev.kind !== 'node_verified' && ev.kind !== 'node_failed') return nodes;
  const label = ev.label ?? '';
  if (!label) return nodes;
  const status: NodeStatusChip =
    ev.kind === 'node_verified' ? 'verified' : 'failed';
  let changed = false;
  const out = nodes.map((n) => {
    if (n.label !== label || n.status === status) return n;
    changed = true;
    return { ...n, status };
  });
  return changed ? out : nodes;
}

/**
 * The rollback playback target: the index of the node whose label matches
 * the rollback target (checkpoint nodes carry the user-visible label the
 * governance event echoes). `null` when the label is unknown — the
 * playback honestly stays disabled.
 */
export function rollbackTargetIndex(
  nodes: WorkflowNodeChip[],
  targetLabel: string | null,
): number | null {
  if (targetLabel === null) return null;
  for (let i = nodes.length - 1; i >= 0; i -= 1) {
    if (nodes[i].label === targetLabel) return i;
  }
  return null;
}
