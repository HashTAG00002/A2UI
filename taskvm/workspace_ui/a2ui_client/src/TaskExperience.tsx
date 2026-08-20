/**
 * TaskExperience — the island's composition root.
 *
 * Everything the user sees is driven by server truth:
 *  - §20.1 progress events (goal / t1 / t2 / ready / …) drive the morph
 *    chain through the pure reducer (`a2ui/morph.ts`) — no local timers,
 *    no faked skeletons;
 *  - named `governance` SSE events (A7, frozen contract with agentAPP.6)
 *    drive the motion layer; the verified snake trajectory, checkpoint
 *    rewards, the final-PASS-only celebration, rollback reverse
 *    playback and pause honesty;
 *  - ordered A2UI messages feed the official MessageProcessor;
 *  - renderer actions cross the actionBridge → the ONLY write path.
 *
 * A9.1 (owner 2026-08-19: "点 start 没反应 / 太卡 / 图片丢失"):
 *  - governance buttons now POST through the single-sid proxy
 *    (`a2ui/governanceApi.ts`) with OPTIMISTIC receipts (<100ms, a
 *    pending chip + aria-busy on the button) and honest ROLLBACK on
 *    failure — "no reaction to Start" is a bug, not a state;
 *  - a SWR snapshot (`a2ui/snapshotCache.ts`) hydrates the island from
 *    the last rendered state + a "同步中" badge — never a blank page;
 *  - the staged timeline (`progressive/StageTimeline.tsx`) shows the
 *    compile chain with LIVE timers, stamped by real signals;
 *  - the thumbnail pipeline (`a2ui/liveShot.ts`) feeds the A9.2
 *    screenshot wall (`wall/SurfaceWall.tsx`): ≤240px thumbs, hash
 *    dedup (unchanged screen ⇒ zero bytes), 150ms burst coalescing,
 *    adaptive slow-network cadence.
 *
 * Governance intents land on the SAME driver/kernel paths the frozen
 * shell's routes run (the proxy is the composition seam; the SSE
 * governance events remain the authoritative state source).
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
import { StageTimeline, type StageMark } from './progressive/StageTimeline';
import { SurfaceWall, type WallLane } from './wall/SurfaceWall';
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
import { postGovernance, type GovCommand } from './a2ui/governanceApi';
import { loadSnapshot, saveSnapshot } from './a2ui/snapshotCache';
import { useLiveShot } from './a2ui/liveShot';
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
  // ── SWR snapshot hydration (A9.1): the last rendered state first,
  //    a "同步中" badge until the first live server signal lands —
  //    entering a session NEVER shows a blank loading page.
  const [state, setState] = useState<IslandState>(() => {
    const snap = loadSnapshot();
    if (snap) {
      return {
        phase: snap.phase,
        status: snap.status,
        goal: snap.goal,
        skeletons: snap.skeletons,
        nodes: snap.nodes,
        streamError: null,
      };
    }
    return INITIAL_ISLAND_STATE;
  });
  const hydratedFromCache = useRef(loadSnapshot() !== null);
  const [syncing, setSyncing] = useState(hydratedFromCache.current);
  const [lastAction, setLastAction] = useState<string | null>(null);
  const [connState, setConnState] =
    useState<A2uiConnectionState>('connecting');
  const [localCheckpoints, setLocalCheckpoints] = useState<CheckpointChip[]>(
    [],
  );
  const [evidenceCount] = useState(2);
  const [stageMarks, setStageMarks] = useState<StageMark[]>([]);
  const [govPending, setGovPending] = useState<readonly GovCommand[]>([]);
  const [wallSurfaces, setWallSurfaces] = useState<
    { name: string; role: string }[]
  >([]);

  // ── the A7 motion layer (governance SSE events → pure reducer) ───────
  const [motion, setMotion] = useState<GovernanceMotionState>(
    INITIAL_GOVERNANCE_MOTION,
  );
  // the snake's own node list: unlike the T2 plane (frozen once live),
  // the trajectory keeps consuming t2 node updates through the whole
  // run — that is exactly what "verified progress" means
  const [snakeNodes, setSnakeNodes] = useState<WorkflowNodeChip[]>([]);

  // ── A9.2: the live-shot thumbnail pipeline → the screenshot wall ────
  const liveShot = useLiveShot(true);

  // The processor is created inside useA2uiStream; the action handler
  // needs it (to read the client-side data model) BEFORE the hook's
  // return value exists — a ref bridges the declaration order. The hook
  // stores the handler in a ref too, so it always fires with the latest.
  const processorRef = useRef<ReturnType<
    typeof useA2uiStream
  >['processor'] | null>(null);

  const stateRef = useRef(state);
  stateRef.current = state;

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
    // instead. Equivalent, honest paths; never a guessed value.
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
    // the first LIVE signal retires the SWR "syncing" badge
    setSyncing(false);
    // stamp the staged timeline (A9.1) — real signal arrivals only
    setStageMarks((marks) => {
      const key = ev.stage as StageMark['key'];
      if (key === 'goal') return [{ key: 'goal', at: Date.now(), label: '接收任务' }];
      if (key === 't1' || key === 't2' || key === 'ready' || key === 'failed') {
        if (marks.some((m) => m.key === key)) return marks;
        return [...marks, { key, at: Date.now(), label: '' }];
      }
      return marks;
    });
    setState((s) => reduceProgress(s, ev as Parameters<
      typeof reduceProgress
    >[1]));
    if (ev.stage === 'goal' || ev.stage === 'goal_failed') {
      // a new instruction (or its failure) resets the motion layer too
      setMotion(INITIAL_GOVERNANCE_MOTION);
      setSnakeNodes([]);
      if (ev.stage === 'goal') setLocalCheckpoints([]);
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
    // server governance truth retires the syncing badge too
    setSyncing(false);
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
    // lifecycle verdicts map onto the shell status pill — the server's
    // governance truth is AUTHORITATIVE: it wins over local optimism
    // AND over its rollback (a locally refused start does not make a
    // later server-side pause a lie — the world paused anyway)
    setState((s) => {
      switch (ev.kind) {
        case 'pause':
          return { ...s, status: 'paused' };
        case 'resume':
          return { ...s, status: 'running' };
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
      onMessages: (messages) => {
        setSyncing(false);   // ordered protocol truth arrived
        processMessages(messages);
      },
      onProgress: handleProgress,
      onGovernance: handleGovernance,
      onConnectionChange: setConnState,
    });
    // Refresh recovery: the fixed APP shell's public status route is the
    // authority for the goal text while no progress event has arrived
    // yet (e.g. the island was opened directly after a goal started) —
    // AND the world's surface names feed the A9.2 screenshot wall.
    fetchAppStatus().then((st) => {
      const goals = st?.goals ?? [];
      const last = goals[goals.length - 1];
      if (last) {
        setState((s) => (s.goal ? s : { ...s, goal: last.goal }));
      }
    });
    fetch('/api/app/surface_shots')
      .then((r) => r.json())
      .then((feed: { surfaces?: { name: string; role: string }[] }) => {
        if (Array.isArray(feed.surfaces) && feed.surfaces.length > 0) {
          setWallSurfaces(feed.surfaces);
        }
      })
      .catch(() => {
        // the wall degrades to the feed entry alone — honest, not fatal
      });
    return () => conn.close();
  }, [processMessages, handleProgress, handleGovernance]);

  // The A2UI messages themselves are the authoritative surface-exists
  // evidence (reconnect/replay path — the progress ring may overflow).
  const hasSurfaces = stream.surfaces.length > 0;
  useEffect(() => {
    if (hasSurfaces) setState((s) => withSurfacesLive(s));
  }, [hasSurfaces]);

  // ── persist the SWR snapshot (debounced; visible fields only) ──────
  useEffect(() => {
    const id = setTimeout(() => {
      saveSnapshot({
        goal: state.goal,
        phase: state.phase,
        status: state.status,
        skeletons: state.skeletons,
        nodes: state.nodes,
        checkpoints: [],
      });
    }, 500);
    return () => clearTimeout(id);
  }, [state.goal, state.phase, state.status, state.skeletons, state.nodes]);

  // ── governance commands (A9.1: optimistic receipt + honest rollback).
  //    The optimistic status flips IMMEDIATELY (same render cycle as
  //    the click — the <100ms receipt); a `{ok:false}` answer rolls
  //    back to the pre-click status and surfaces the reason verbatim.
  const runGovernance = useCallback(
    (command: GovCommand, label?: string) => {
      const optimistic: Partial<Record<GovCommand, IslandState['status']>> = {
        start: 'running',
        pause: 'paused',
        resume: 'running',
        stop: 'failed',
      };
      const t0 = performance.now();
      if (typeof performance.mark === 'function') {
        performance.mark(`gov-${command}-click`);
      }
      setGovPending((p) => (p.includes(command) ? p : [...p, command]));
      const prevStatus = stateRef.current.status;
      const next = optimistic[command];
      if (next) setState((s) => ({ ...s, status: next }));
      postGovernance(command, label).then((res) => {
        if (typeof performance.mark === 'function') {
          performance.mark(`gov-${command}-ack`);
          try {
            performance.measure(
              `gov-${command}`,
              `gov-${command}-click`,
              `gov-${command}-ack`,
            );
          } catch {
            // marks are best-effort observability
          }
        }
        setGovPending((p) => p.filter((c) => c !== command));
        if (res.ok) {
          setLastAction(
            `${command} ✓${res.state ? ` (${res.state})` : ''}`
              + (label ? ` · ${label}` : ''),
          );
        } else {
          // honest rollback: restore the pre-click status, show why
          setState((s) =>
            s.status === next && prevStatus !== next
              ? { ...s, status: prevStatus }
              : s,
          );
          setLastAction(`${command} ✗ ${res.error ?? '失败（已回滚）'}`);
        }
        void t0;
      });
    },
    [],
  );

  const canStart = state.phase === 'live' && state.status === 'ready';
  const onStart = useCallback(() => {
    if (!canStart) return;
    runGovernance('start');
  }, [canStart, runGovernance]);
  const onPause = useCallback(
    () => runGovernance('pause'),
    [runGovernance],
  );
  const onResume = useCallback(
    () => runGovernance('resume'),
    [runGovernance],
  );
  const onStop = useCallback(() => runGovernance('stop'), [runGovernance]);

  const onCheckpoint = useCallback(() => {
    // the label is user-visible by construction (the server mints the
    // same naming scheme when the body omits one)
    const n = localCheckpoints.length + motion.checkpoints.length + 1;
    const label = `检查点 ${n}`;
    setLocalCheckpoints((cps) => [...cps, { label }]);
    runGovernance('checkpoint', label);
  }, [localCheckpoints.length, motion.checkpoints.length, runGovernance]);

  const onRollback = useCallback(() => {
    const all = [...localCheckpoints, ...motion.checkpoints];
    const last = all[all.length - 1];
    if (last) runGovernance('rollback', last.label);
  }, [localCheckpoints, motion.checkpoints, runGovernance]);

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
  const verifiedCount = snakeNodes.filter((n) => n.status === 'verified').length;
  const wallLanes: WallLane[] = useMemo(
    () => snakeNodes.map((n) => ({
      label: n.label, kind: n.kind, status: n.status,
    })),
    [snakeNodes],
  );

  return (
    <GovernanceShell
      goal={state.goal || WAITING_GOAL_TEXT}
      status={state.status}
      canStart={canStart}
      pendingActions={govPending}
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
      syncing={syncing}
    >
      <StageTimeline
        marks={stageMarks}
        verifiedCount={verifiedCount}
        totalCount={snakeNodes.length}
        executing={state.status === 'running'}
      />
      <SurfaceWall
        entry={liveShot.entry}
        thumbUrl={liveShot.url}
        loading={liveShot.loading}
        surfaces={wallSurfaces}
        lanes={wallLanes}
      />
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
              <p className="last-action" data-testid="last-action" role="status">
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
