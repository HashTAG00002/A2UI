/**
 * actionBridge — renderer actions leave the island as STRUCTURED events.
 *
 * The dynamic task surface's only action this wave is
 * `taskvm.local_patch` with `{semanticKey, value?}` context. The bridge
 * maps the raw `A2uiClientAction` onto a typed event and refuses
 * everything else (unknown actions are surfaced as a rejected marker —
 * the same no-best-effort-guessing rule as the backend policy layer).
 */
import { ACTION_LOCAL_PATCH } from './protocol';
import type { A2uiClientAction } from '@a2ui/web_core/v0_9';

export interface TaskvmLocalPatchEvent {
  kind: 'taskvm.local_patch';
  surfaceId: string;
  sourceComponentId: string;
  semanticKey: string;
  value?: unknown;
}

export interface RejectedActionEvent {
  kind: 'rejected';
  surfaceId: string;
  reason: string;
  actionName: string;
}

export type SurfaceActionEvent = TaskvmLocalPatchEvent | RejectedActionEvent;

/** Governance actions NEVER travel this bridge — they live in the shell. */
export const GOVERNANCE_ACTIONS = [
  'start',
  'pause',
  'resume',
  'stop',
  'checkpoint',
  'rollback',
  'goal_patch',
  'recompose',
  'resolve_conflict',
] as const;

export function translateAction(action: A2uiClientAction): SurfaceActionEvent {
  const base = {
    surfaceId: action.surfaceId,
    sourceComponentId: action.sourceComponentId,
  };

  if (action.name === ACTION_LOCAL_PATCH) {
    const semanticKey = action.context?.semanticKey;
    if (typeof semanticKey !== 'string' || !semanticKey) {
      return {
        ...base,
        kind: 'rejected',
        actionName: action.name,
        reason: 'taskvm.local_patch requires a non-empty context.semanticKey',
      };
    }
    const event: TaskvmLocalPatchEvent = {
      ...base,
      kind: ACTION_LOCAL_PATCH,
      semanticKey,
    };
    if ('value' in (action.context ?? {})) {
      event.value = action.context.value;
    }
    return event;
  }

  if ((GOVERNANCE_ACTIONS as readonly string[]).includes(action.name)) {
    return {
      ...base,
      kind: 'rejected',
      actionName: action.name,
      reason: 'governance actions belong to the fixed shell, not the dynamic surface',
    };
  }

  return {
    ...base,
    kind: 'rejected',
    actionName: action.name,
    reason: `unknown action ${action.name}`,
  };
}
