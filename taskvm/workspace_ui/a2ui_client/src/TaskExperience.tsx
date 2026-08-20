/**
 * TaskExperience — the island's composition root.
 *
 * Everything the user sees is driven by server truth:
 *  - §20.1 progress events (goal / t1 / t2 / ready / …) drive the morph
 *    chain through the pure reducer (`a2ui/morph.ts`) — no local timers,
 *    no faked skeletons;
 *  - named `governance` SSE events (A7, frozen contract with agentAPP.6:
 *    GOVERNANCE_SSE_KINDS on the server, GOVERNANCE_KINDS here — the
 *    same closed 10-kind vocabulary) drive the motion layer through
 *    `a2ui/governanceEvents.ts`: the verified snake trajectory,
 *    checkpoint rewards, the final-PASS-only celebration, rollback
 *    reverse playback and pause honesty;
 *  - ordered A2UI messages feed the official MessageProcessor;
 *  - renderer actions cross the actionBridge: the edited value is read
 *    from the CLIENT-side data model (the GenericBinder's generated
 *    setValue → dataContext.set wrote it there on every keystroke) and
 *    POSTed to the ONLY write path (/api/app/a2ui/action → policy
 *    re-check → ONE governance local_patch). The zero-model-call
 *    updateDataModel frame from the server poller is how the new value
 *    lands back on screen — the A5 acceptance loop.
 *
 * The A6 IntentConsole (fixed chrome) POSTs free text to
 * /api/app/a2ui/intent and renders the structured answer — it never
 * writes anything itself.
 *
 * Governance intents (start/pause/…) stay local-shell behavior this
 * wave (wiring them to the public governance routes is a separate
 * card); the shell chrome itself is untouched.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  GovernanceShell,
  type CheckpointChip,
} from './governance/GovernanceShell';
import {
  ProgressiveTaskPlane,
  type WorkflowNodeChip,
} from './progressive/ProgressiveTaskPlane';
import { SnakeProgress } from './progressive/SnakeProgress';
import { CelebrationLayer } from './motion/CelebrationLayer';
import { IntentConsole } from './intent/IntentConsole';
import type { IntentResponse } from './intent/intentTransport';
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
  toChip,
  withSurfacesLive,
  type IslandState,
} from './a2ui/morph';
import {
  INITIAL_GOVERNANCE_MOTION,
  reduceGovernance,
  type GovernanceMotionState,
} from './a2ui/governanceEvents';
import { applyGovernanceToNodes } from './a2ui/snake';

const WAITING_GOAL_TEXT = '等待第一条任务指令…';

export function TaskExperience() {
  const [state, setState] = useState<IslandState>(INITIAL_ISLAND_STATE);
  const [lastAction, setLastAction] = useState<string | null>(null);
  const [connState, setConnState] =
    useState<A2uiConnectionState>('connecting');
  const [localCheckpoints, setLocalCheckpoints] = useState<CheckpointChip[]>(
    [],
  );
  const [evidenceCount] = useState(2);

  // ── the A7 motion layer (governance SSE events → pure reducer) ───────
  const [motion, setMotion] = useState<GovernanceMotionState>(
    INITIAL_GOVERNANCE_MOTION,
  );
  // the snake's own node list: unlike the T2 plane (frozen once live),
  // the trajectory keeps consuming t2 node updates through the whole
  // run — that is exactly what "verified progress" means
  const [snakeNodes, setSnakeNodes] = useState<WorkflowNodeChip[]>([]);

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
    // The renderer hands us the action context AFTER resolving data
    // bindings (web_core generic-binder resolveDeepSync) — a
    // protocol-native {"path": …} value in the tree arrives here as a
    // literal (policy-side: ticket A5-IFACE-01, adjudicated 2026-08-20).
    // Trees that omit the value (semanticKey-only, the baseline form)
    // read the CURRENT edited value from the client-side data model
    // instead — the GenericBinder's setValue wrote it there on every
    // keystroke. Equivalent, honest paths; never a guessed value.
    const surface = processorRef.current?.model.surfacesMap.get(
      action.surfaceId,
    );
    let value = event.value;
    if (value === undefined) {
      value = surface?.dataModel.get(
        `/variables/${event.semanticKey}/desired`,
      );
    }
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

  // ── progress signals: the morph chain + the snake's node truth ──────
  const handleProgress = useCallback((ev: {
    stage: string;
    goal?: string;
    nodes?: { label: string; kind: string; status: string }[];
  }) => {
    setState((s) => reduceProgress(s, ev as Parameters<
      typeof reduceProgress
    >[1]));
    if (ev.stage === 'goal') {
      // a new instruction resets the motion layer honestly too
      setMotion(INITIAL_GOVERNANCE_MOTION);
      setSnakeNodes([]);
      return;
    }
    if (ev.stage === 't2' && Array.isArray(ev.nodes)) {
      setSnakeNodes(ev.nodes.map(toChip));
    }
  }, []);

  // ── governance signals: the motion state machine ────────────────────
  const handleGovernance = useCallback((ev: {
    kind: string;
    label?: string;
  }) => {
    setMotion((m) => reduceGovernance(m, ev as Parameters<
      typeof reduceGovernance
    >[1]));
    if (ev.kind === 'node_verified' || ev.kind === 'node_failed') {
      setSnakeNodes((nodes) =>
        applyGovernanceToNodes(nodes, ev as Parameters<
          typeof applyGovernanceToNodes
        >[1]),
      );
    }
    // terminal / lifecycle verdicts map onto the shell status pill —
    // the server's governance truth wins over local optimism
    setState((s) => {
      switch (ev.kind) {
        case 'pause':
          return s.status === 'running' ? { ...s, status: 'paused' } : s;
        case 'resume':
          return s.status === 'paused' ? { ...s, status: 'running' } : s;
        case 'stop':
          return { ...s, status: 'failed' };
        case 'final_pass':
          return { ...s, status: 'completed' };
        case 'final_fail':
          return { ...s, status: 'failed' };
        default:
          return s;
      }
    });
  }, []);

  // ── the REAL transport: one SSE connection for all three streams ──
  const processMessages = stream.processMessages;
  useEffect(() => {
    const conn = connectA2ui({
      onMessages: (messages) => processMessages(messages),
      onProgress: handleProgress,
      onGovernance: handleGovernance,
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
  }, [processMessages, handleProgress, handleGovernance]);

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
    setLocalCheckpoints((cps) => [...cps, { label: `检查点 ${cps.length + 1}` }]);
  }, []);

  const onRollback = useCallback(() => {
    setLocalCheckpoints((cps) => cps.slice(0, -1));
    setLastAction('rollback');
  }, []);

  const onOpenEvidence = useCallback(() => setLastAction('open-evidence'), []);
  const onOpenSubstrate = useCallback(() => setLastAction('open-substrate'), []);

  // ── A7 wiring ────────────────────────────────────────────────────────
  const onRollbackDone = useCallback(() => {
    setMotion((m) =>
      m.rollback.active
        ? { ...m, rollback: { ...m.rollback, active: false } }
        : m,
    );
  }, []);

  const onIntentSubmit = useCallback(
    (response: IntentResponse, text: string) => {
      // the console only notifies on ok:true answers — narrow for the
      // echo (the ok:false branch is its own error surface)
      const kind = response.ok ? response.kind : 'error';
      setLastAction(`intent(${kind}): ${text.slice(0, 60)}`);
    },
    [],
  );

  // the checkpoint strip: local shell clicks + server governance events,
  // deduped by label (both are user-visible truth)
  const checkpoints: CheckpointChip[] = useMemo(() => {
    const seen = new Set<string>();
    const out: CheckpointChip[] = [];
    for (const c of [...localCheckpoints, ...motion.checkpoints]) {
      if (seen.has(c.label)) continue;
      seen.add(c.label);
      out.push({ label: c.label, reached: c.reached });
    }
    return out;
  }, [localCheckpoints, motion.checkpoints]);

  const showSnake = state.phase === 'live' && snakeNodes.length > 0;

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
      intentConsole={<IntentConsole onSubmit={onIntentSubmit} />}
    >
      <ProgressiveTaskPlane
        phase={state.phase}
        variableSkeletons={state.skeletons}
        workflowNodes={state.nodes as WorkflowNodeChip[]}
        live={
          <>
            {showSnake && (
              <SnakeProgress
                nodes={snakeNodes}
                frozen={motion.paused}
                playback={motion.rollback.active ? motion.rollback : null}
                onRollbackDone={onRollbackDone}
              />
            )}
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
      <CelebrationLayer celebration={motion.celebration} />
    </GovernanceShell>
  );
}
