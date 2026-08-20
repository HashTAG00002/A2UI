/**
 * GovernanceShell — the FIXED, system-controlled chrome (workplan §3).
 *
 * Iron laws (test-pinned):
 *  1. Goal, Start/Pause/Resume/Stop, Checkpoint, Rollback, Evidence,
 *     Live substrate and the status pill are ALWAYS in the DOM — never
 *     created, hidden, replaced or reordered by the model-generated
 *     surface. State only flips `disabled`.
 *  2. During compilation (T0/T1/T2) Start stays DISABLED: "Ready, not
 *     autostart" — the user must see the surface before autonomy starts.
 *  3. Controls emit structured governance intents only; nothing here
 *     routes through the dynamic A2UI surface.
 */
import type { ReactNode } from 'react';
import { StatusPill } from './StatusPill';

export type TaskStatus =
  | 'compiling'
  | 'ready'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed';

export interface CheckpointChip {
  /** User-visible label only — internal ids never enter the island. */
  label: string;
  /** reached checkpoints carry the A7 small-reward visual state */
  reached?: boolean;
}

export interface GovernanceShellProps {
  goal: string;
  status: TaskStatus;
  /** False while the task world is still compiling (T0–T2). */
  canStart: boolean;
  /** Commands whose optimistic POST is still in flight (A9.1): the
   *  button flips to its pending chip (label + "…") — a <100ms VISIBLE
   *  receipt for every governance click. Optional + backward compatible. */
  pendingActions?: readonly string[];
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onStop: () => void;
  onCheckpoint: () => void;
  onRollback: () => void;
  checkpoints: CheckpointChip[];
  evidenceCount: number;
  onOpenEvidence: () => void;
  substrateLabel: string;
  onOpenSubstrate: () => void;
  /** The A6 free-text intent console — FIXED shell chrome, never
   *  model-generated, rendered between the goal card and the controls. */
  intentConsole?: ReactNode;
  /** True while the island renders the SWR snapshot and the first live
   *  server signal has not landed yet (A9.1) — shows the "同步中"
   *  badge. Optional + backward compatible. */
  syncing?: boolean;
  /** The dynamic task region (model-generated A2UI surface / skeleton). */
  children: ReactNode;
}

function GovButton({
  action,
  label,
  onClick,
  disabled,
  tone,
  pending,
}: {
  action: string;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  tone?: 'primary' | 'danger';
  pending?: boolean;
}) {
  const cls = tone === 'primary' ? 'gov-btn gov-btn--primary' : tone === 'danger' ? 'gov-btn gov-btn--danger' : 'gov-btn';
  return (
    <button
      type="button"
      className={cls}
      data-governance-action={action}
      onClick={onClick}
      disabled={disabled}
      aria-busy={pending ? 'true' : undefined}
      data-pending={pending ? 'true' : 'false'}
    >
      {label}
      {pending ? '…' : ''}
    </button>
  );
}

export function GovernanceShell({
  goal,
  status,
  canStart,
  pendingActions = [],
  onStart,
  onPause,
  onResume,
  onStop,
  onCheckpoint,
  onRollback,
  checkpoints,
  evidenceCount,
  onOpenEvidence,
  substrateLabel,
  onOpenSubstrate,
  intentConsole,
  syncing = false,
  children,
}: GovernanceShellProps) {
  const started = status === 'running' || status === 'paused';
  const finished = status === 'completed' || status === 'failed';
  const isPending = (a: string) => pendingActions.includes(a);

  return (
    <div className="shell" data-testid="governance-shell">
      <header className="shell__bar">
        <div className="shell__brand">
          <span className="shell__logo" aria-hidden>◆</span>
          <span className="shell__name">TaskVM</span>
        </div>
        <StatusPill status={status} />
        <button
          type="button"
          className="gov-btn gov-btn--ghost"
          data-governance-action="open-substrate"
          onClick={onOpenSubstrate}
        >
          {substrateLabel}
        </button>
        {syncing && (
          <span className="sync-badge" data-testid="sync-badge">同步中…</span>
        )}
      </header>

      <section className="shell__goal" data-testid="goal-card">
        <h1 className="shell__goal-label">任务目标</h1>
        <p className="shell__goal-text" data-testid="goal-text">{goal}</p>
      </section>

      {intentConsole && (
        <section className="shell__intent" data-testid="intent-console-slot">
          {intentConsole}
        </section>
      )}

      <section className="shell__controls" data-testid="governance-controls">
        <GovButton action="start" label="开始" onClick={onStart}
                   disabled={!canStart || started || finished}
                   pending={isPending('start')} tone="primary" />
        <GovButton action="pause" label="暂停" onClick={onPause}
                   disabled={status !== 'running'} pending={isPending('pause')} />
        <GovButton action="resume" label="继续" onClick={onResume}
                   disabled={status !== 'paused'} pending={isPending('resume')} />
        <GovButton action="stop" label="停止" onClick={onStop}
                   disabled={!started || finished} pending={isPending('stop')}
                   tone="danger" />
        <GovButton action="checkpoint" label="打检查点" onClick={onCheckpoint}
                   disabled={status !== 'running'}
                   pending={isPending('checkpoint')} />
        <GovButton action="rollback" label="回退" onClick={onRollback}
                   disabled={checkpoints.length === 0}
                   pending={isPending('rollback')} />
        <GovButton action="open-evidence" label={`证据 (${evidenceCount})`}
                   onClick={onOpenEvidence} />
      </section>

      {checkpoints.length > 0 && (
        <section className="shell__checkpoints" data-testid="checkpoint-strip">
          {checkpoints.map((cp, i) => (
            <span
              key={`${cp.label}-${i}`}
              className={`checkpoint-chip${cp.reached ? ' checkpoint-chip--reached' : ''}`}
              data-reached={cp.reached ? 'true' : 'false'}
            >
              {cp.label}
            </span>
          ))}
        </section>
      )}

      <main className="shell__dynamic" data-testid="dynamic-task-region">
        {children}
      </main>
    </div>
  );
}
