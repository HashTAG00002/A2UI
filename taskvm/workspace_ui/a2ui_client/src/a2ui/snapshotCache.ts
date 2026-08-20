/**
 * snapshotCache — SWR-style last-known-good snapshot for the island
 * (A9.1: "entering a session must NEVER show a blank loading page").
 *
 * On mount the island hydrates from the LAST rendered state (goal text,
 * compile phase, variable skeletons, workflow chips, checkpoint labels)
 * and shows a "同步中" (syncing) badge until the first LIVE server
 * signal arrives — then the server truth replaces the cache. Screen-
 * visible fields only; no internal ids ever enter storage (repo
 * contract §3), no fabricated values (labels/pending marks only).
 */
import type { CompilePhase } from '../progressive/ProgressiveTaskPlane';
import type { TaskStatus } from '../governance/GovernanceShell';
import type { VariableSkeleton, WorkflowNodeChip } from '../progressive/ProgressiveTaskPlane';

export interface IslandSnapshot {
  v: 1;
  savedAt: number;
  goal: string;
  phase: CompilePhase;
  status: TaskStatus;
  skeletons: VariableSkeleton[];
  nodes: WorkflowNodeChip[];
  checkpoints: string[];
}

const KEY = 'taskvm.island.snapshot.v1';

/** Persist the current visible state (best-effort: a full/absent
 * localStorage degrades to a fresh boot — never an error). */
export function saveSnapshot(s: Omit<IslandSnapshot, 'v' | 'savedAt'>): void {
  try {
    const snap: IslandSnapshot = { v: 1, savedAt: Date.now(), ...s };
    localStorage.setItem(KEY, JSON.stringify(snap));
  } catch {
    // quota / private mode / SSR — caching is an enhancement, never a
    // dependency
  }
}

/** The last saved snapshot, or ``null`` when none is readable. */
export function loadSnapshot(): IslandSnapshot | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as IslandSnapshot;
    if (s?.v !== 1 || typeof s.goal !== 'string') return null;
    return {
      ...s,
      skeletons: Array.isArray(s.skeletons) ? s.skeletons : [],
      nodes: Array.isArray(s.nodes) ? s.nodes : [],
      checkpoints: Array.isArray(s.checkpoints) ? s.checkpoints : [],
    };
  } catch {
    return null;
  }
}

/** Drop the cache (e.g. the server truth landed and replaced it). */
export function clearSnapshot(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // best-effort by design
  }
}
