/**
 * StatusPill — the big, unmissable task status badge (Governance Shell).
 */
import type { TaskStatus } from './GovernanceShell';

const STATUS_LABELS: Record<TaskStatus, string> = {
  compiling: '编译中',
  ready: '已就绪 · 未执行',
  running: '执行中',
  paused: '已暂停',
  completed: '已完成',
  failed: '失败',
};

const STATUS_TONES: Record<TaskStatus, string> = {
  compiling: 'pill pill--compiling',
  ready: 'pill pill--ready',
  running: 'pill pill--running',
  paused: 'pill pill--paused',
  completed: 'pill pill--completed',
  failed: 'pill pill--failed',
};

export function StatusPill({ status }: { status: TaskStatus }) {
  return (
    <span className={STATUS_TONES[status]} data-testid="task-status-pill">
      <span className="pill__dot" aria-hidden />
      {STATUS_LABELS[status]}
    </span>
  );
}
