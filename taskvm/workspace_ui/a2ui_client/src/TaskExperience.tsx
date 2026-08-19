/**
 * TaskExperience — the island's composition root (A5: REAL stream).
 *
 * Everything the user sees is driven by server truth:
 *  - §20.1 progress events (goal / t1 / t2 / ready / …) drive the morph
 *    chain through the pure reducer (`a2ui/morph.ts`) — no local timers,
 *    no faked skeletons;
 *  - ordered A2UI messages feed the official MessageProcessor;
 *  - renderer actions cross the actionBridge: the edited value is read
 *    from the CLIENT-side data model (the GenericBinder's generated
 *    setValue → dataContext.set wrote it there on every keystroke) and
 *    POSTed to the ONLY write path (/api/app/a2ui/action → policy
 *    re-check → ONE governance local_patch). The zero-model-call
 *    updateDataModel frame from the server poller is how the new value
 *    lands back on screen — the A5 acceptance loop.
 *
 * Governance intents (start/pause/…) stay local-shell behavior this
 * wave (wiring them to the public governance routes is a separate
 * card); the shell chrome itself is untouched.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { GovernanceShell } from './governance/GovernanceShell';
import {
  ProgressiveTaskPlane,
  type WorkflowNodeChip,
} from './progressive/ProgressiveTaskPlane';
import { A2uiSurface } from '@a2ui/react/v0_9';
import type { A2uiClientAction } from '@a2ui/web_core/v0_9';
import { useA2uiStream } from './a2ui/useA2uiStream';
import { translateAction } from './a2ui/actionBridge';
import {
  connectA2ui,
  fetchAppStatus,
  postSurfaceAction,
  type A2uiConnectionState,
} from './a2ui/transport';
import {
  INITIAL_ISLAND_STATE,
  reduceProgress,
  withSurfacesLive,
  type IslandState,
} from './a2ui/morph';

const WAITING_GOAL_TEXT = '等待第一条任务指令…';

export function TaskExperience() {
  const [state, setState] = useState<IslandState>(INITIAL_ISLAND_STATE);
  const [lastAction, setLastAction] = useState<string | null>(null);
  const [connState, setConnState] =
    useState<A2uiConnectionState>('connecting');
  const [checkpoints, setCheckpoints] = useState<{ label: string }[]>([]);
  const [evidenceCount] = useState(2);

  // The processor is created inside useA2uiStream; the action handler
  // needs it (to read the client-side data model) BEFORE the hook's
  // return value exists — a ref bridges the declaration order. The hook
  // stores the handler in a ref too, so it always fires with the latest.
  const processorRef = useRef<ReturnType<
    typeof useA2uiStream
  >['processor'] | null>(null);

  const onSurfaceAction = useCallback((action: A2uiClientAction) => {
    const event = translateAction(action);
    if (event.kind === 'rejected') {
      setLastAction(`rejected(${event.actionName}): ${event.reason}`);
      return;
    }
    // The edited value lives in the CLIENT-side data model: the
    // GenericBinder auto-generated setValue (dataContext.set) wrote it
    // there on every keystroke (generic-binder.js "setters"). The
    // protocol-native alternative — binding {"path": …} inside
    // action.event.context — is rejected by the current genui policy
    // layer (ticket A5-IFACE-01: docs/04_RM&APP时代/
    // ticket_A5_IFACE_01_policy_databinding.md); this read is the
    // interim, equally-honest path.
    const surface = processorRef.current?.model.surfacesMap.get(
      action.surfaceId,
    );
    const value = surface?.dataModel.get(
      `/variables/${event.semanticKey}/desired`,
    );
    if (value === undefined) {
      setLastAction(
        `rejected(${event.kind}): 读不到 ${event.semanticKey} 的编辑值`,
      );
      return;
    }
    postSurfaceAction(event.kind, {
      semanticKey: event.semanticKey,
      value,
    }).then((res) => {
      setLastAction(
        res.ok
          ? `local_patch(${event.semanticKey}) 已提交`
          : `local_patch(${event.semanticKey}) ✗ ${res.error ?? '失败'}`,
      );
    });
  }, []);

  const stream = useA2uiStream(onSurfaceAction);

  useEffect(() => {
    processorRef.current = stream.processor;
  }, [stream.processor]);

  // ── the REAL transport: one SSE connection for both streams ────────
  const processMessages = stream.processMessages;
  useEffect(() => {
    const conn = connectA2ui({
      onMessages: (messages) => processMessages(messages),
      onProgress: (ev) => setState((s) => reduceProgress(s, ev)),
      onConnectionChange: setConnState,
    });
    // Refresh recovery: the fixed APP shell's public status route is the
    // authority for the goal text while no progress event has arrived
    // yet (e.g. the island was opened directly after a goal started).
    fetchAppStatus().then((st) => {
      const goals = st?.goals ?? [];
      const last = goals[goals.length - 1];
      if (!last) return;
      setState((s) => (s.goal ? s : { ...s, goal: last.goal }));
    });
    return () => conn.close();
  }, [processMessages]);

  // The A2UI messages themselves are the authoritative surface-exists
  // evidence (reconnect/replay path — the progress ring may overflow).
  const hasSurfaces = stream.surfaces.length > 0;
  useEffect(() => {
    if (hasSurfaces) setState((s) => withSurfacesLive(s));
  }, [hasSurfaces]);

  // ── governance shell intents (local this wave — separate card) ─────
  const canStart = state.phase === 'live' && state.status === 'ready';

  const onStart = useCallback(() => {
    if (!canStart) return;
    setState((s) => ({ ...s, status: 'running' }));
    setLastAction('start');
  }, [canStart]);

  const onPause = useCallback(
    () =>
      setState((s) => ({ ...s, status: s.status === 'running' ? 'paused' : s.status })),
    [],
  );
  const onResume = useCallback(
    () =>
      setState((s) => ({ ...s, status: s.status === 'paused' ? 'running' : s.status })),
    [],
  );
  const onStop = useCallback(() => {
    setState((s) => ({
      ...s,
      status: s.status === 'running' || s.status === 'paused' ? 'failed' : s.status,
    }));
    setLastAction('stop');
  }, []);

  const onCheckpoint = useCallback(() => {
    setCheckpoints((cps) => [...cps, { label: `检查点 ${cps.length + 1}` }]);
  }, []);

  const onRollback = useCallback(() => {
    setCheckpoints((cps) => cps.slice(0, -1));
    setLastAction('rollback');
  }, []);

  const onOpenEvidence = useCallback(() => setLastAction('open-evidence'), []);
  const onOpenSubstrate = useCallback(() => setLastAction('open-substrate'), []);

  return (
    <GovernanceShell
      goal={state.goal || WAITING_GOAL_TEXT}
      status={state.status}
      canStart={canStart}
      onStart={onStart}
      onPause={onPause}
      onResume={onResume}
      onStop={onStop}
      onCheckpoint={onCheckpoint}
      onRollback={onRollback}
      checkpoints={checkpoints}
      evidenceCount={evidenceCount}
      onOpenEvidence={onOpenEvidence}
      substrateLabel="MobileGym"
      onOpenSubstrate={onOpenSubstrate}
    >
      <ProgressiveTaskPlane
        phase={state.phase}
        variableSkeletons={state.skeletons}
        workflowNodes={state.nodes as WorkflowNodeChip[]}
        live={
          <>
            {stream.surfaces.map((surface) => (
              <A2uiSurface key={surface.id} surface={surface} />
            ))}
            {connState !== 'open' && (
              <p className="conn-state" data-testid="conn-state">
                {connState === 'reconnecting' ? '连接中断，正在重连…' : '连接中…'}
              </p>
            )}
            {state.streamError && (
              <p className="stream-error" data-testid="stream-error">
                {state.streamError}
              </p>
            )}
            {lastAction && (
              <p className="last-action" data-testid="last-action">
                {lastAction}
              </p>
            )}
          </>
        }
      />
    </GovernanceShell>
  );
}
