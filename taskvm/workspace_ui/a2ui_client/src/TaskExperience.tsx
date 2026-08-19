/**
 * TaskExperience — the island's composition root (wave A5: mock-driven).
 *
 * Owns the compile-chain progression (T0 → T1 → T2 → live) fed by MOCK
 * data this wave; the SSE transport (next wave) replaces the timers with
 * real bootstrap/stream events — the component tree does not change,
 * only where `advance*` / `processMessages` get called from.
 *
 * Governance intents (start/pause/…) stay in the shell; the dynamic
 * surface's actions cross the actionBridge as structured events.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { GovernanceShell, type TaskStatus } from './governance/GovernanceShell';
import {
  ProgressiveTaskPlane,
  type CompilePhase,
  type WorkflowNodeChip,
} from './progressive/ProgressiveTaskPlane';
import { A2uiSurface } from '@a2ui/react/v0_9';
import { useA2uiStream } from './a2ui/useA2uiStream';
import { translateAction } from './a2ui/actionBridge';
import { mockA2uiMessages } from './a2ui/mockMessages';

const MOCK_GOAL = '把发布会日期改到 8 月底并通知所有参会人';

/** Mock compile-chain returns (T1/T2 payloads — labels only, honest). */
const MOCK_VARIABLE_SKELETONS = [
  { label: '发布日期' },
  { label: '通知名单' },
  { label: '预算' },
];

const MOCK_WORKFLOW_NODES: WorkflowNodeChip[] = [
  { label: '修改发布日期', kind: 'step', status: 'waiting' },
  { label: '日期确认点', kind: 'checkpoint', status: 'waiting' },
  { label: '校验通知名单', kind: 'verification', status: 'waiting' },
];

const SUBSTRATE_LABEL = 'MobileGym';

export function TaskExperience() {
  const [phase, setPhase] = useState<CompilePhase>('t0');
  const [status, setStatus] = useState<TaskStatus>('compiling');
  const [checkpoints, setCheckpoints] = useState<{ label: string }[]>([]);
  const [evidenceCount] = useState(2);
  const [lastAction, setLastAction] = useState<string | null>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  const onSurfaceAction = useCallback((action: Parameters<typeof translateAction>[0]) => {
    const event = translateAction(action);
    if (event.kind === 'taskvm.local_patch') {
      setLastAction(`local_patch(${event.semanticKey})`);
    } else {
      setLastAction(`rejected(${event.actionName}): ${event.reason}`);
    }
  }, []);

  const stream = useA2uiStream(onSurfaceAction);

  // Mock compile chain: T0 → T1 → T2 → live, then the A2UI stream
  // replaces the skeleton. Timers stand in for compiler/architect/
  // decoder returns; the real transport swaps them out next wave.
  useEffect(() => {
    const t1 = setTimeout(() => setPhase('t1'), 350);
    const t2 = setTimeout(() => setPhase('t2'), 800);
    const t3 = setTimeout(() => {
      stream.processMessages(mockA2uiMessages);
      setPhase('live');
      setStatus('ready');
    }, 1400);
    timers.current.push(t1, t2, t3);
    return () => {
      timers.current.forEach(clearTimeout);
      timers.current = [];
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const canStart = phase === 'live' && status === 'ready';

  const onStart = useCallback(() => {
    if (!canStart) return;
    setStatus('running');
    setLastAction('start');
  }, [canStart]);

  const onPause = useCallback(() => setStatus((s) => (s === 'running' ? 'paused' : s)), []);
  const onResume = useCallback(() => setStatus((s) => (s === 'paused' ? 'running' : s)), []);
  const onStop = useCallback(() => {
    setStatus((s) => (s === 'running' || s === 'paused' ? 'failed' : s));
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
      goal={MOCK_GOAL}
      status={status}
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
      substrateLabel={SUBSTRATE_LABEL}
      onOpenSubstrate={onOpenSubstrate}
    >
      <ProgressiveTaskPlane
        phase={phase}
        variableSkeletons={MOCK_VARIABLE_SKELETONS}
        workflowNodes={MOCK_WORKFLOW_NODES}
        live={
          <>
            {stream.surfaces.map((surface) => (
              <A2uiSurface key={surface.id} surface={surface} />
            ))}
            {lastAction && (
              <p className="last-action" data-testid="last-action">{lastAction}</p>
            )}
          </>
        }
      />
    </GovernanceShell>
  );
}
