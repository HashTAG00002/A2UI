/**
 * StageTimeline — the staged progress timeline (A9.1): the compile
 * chain is a 60-100s window and the owner must SEE where the time
 * goes, with LIVE timers:
 *
 *   编译任务世界 ✓ 3.2s → 生成计划 ✓ 11.8s → 执行 2/5 步 (18s…)
 *
 * Honesty rules (test-pinned):
 *  - every stage mark is stamped by a REAL server signal (the parent
 *    stamps `at` when the progress event arrives — no fabricated
 *    timestamps, no guessed stages);
 *  - the live timer ticks ONLY for the current open stage; completed
 *    stages show their measured duration;
 *  - node counters (执行 2/5 步) come from the A7 governance/node
 *    truth — never an estimate.
 */
import { motion, useReducedMotion } from 'motion/react';
import { useEffect, useState } from 'react';

export type StageKey = 'goal' | 't1' | 't2' | 'ready' | 'failed';

export interface StageMark {
  key: StageKey;
  /** When the server signal ARRIVED (parent-stamped, ms epoch). */
  at: number;
  label: string;
}

export interface StageTimelineProps {
  marks: StageMark[];
  /** Verified/total workflow nodes (server truth; 0/0 = not started). */
  verifiedCount?: number;
  totalCount?: number;
  /** Whether the execution stage is open (drives the live timer). */
  executing?: boolean;
}

const STAGE_LABELS: Record<StageKey, string> = {
  goal: '接收任务',
  t1: '编译任务世界',
  t2: '生成计划',
  ready: '就绪',
  failed: '失败',
};

function fmt(ms: number): string {
  const s = ms / 1000;
  return s >= 60 ? `${Math.floor(s / 60)}m${Math.round(s % 60)}s`
    : `${s.toFixed(1)}s`;
}

export function StageTimeline({
  marks,
  verifiedCount = 0,
  totalCount = 0,
  executing = false,
}: StageTimelineProps) {
  const reduced = useReducedMotion() ?? false;
  const [now, setNow] = useState(() => Date.now());

  // NB: every hook runs BEFORE the empty-marks early return — a
  // marks-length change must never change the hook count (React law).
  const last = marks[marks.length - 1];
  const open = marks.length > 0
    && (executing || last.key === 't1' || last.key === 't2');

  useEffect(() => {
    if (!open) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [open]);

  if (marks.length === 0) return null;

  return (
    <section
      className="stage-timeline"
      data-testid="stage-timeline"
      aria-label="阶段进度"
    >
      <ol className="stage-timeline__list">
        {marks.map((m, i) => {
          const prev = i > 0 ? marks[i - 1] : null;
          const dur = prev ? m.at - prev.at : 0;
          const isCurrent = m === last && open;
          const live = isCurrent ? now - m.at : 0;
          const done = m.key === 'ready' || m.key === 'failed'
            || (m !== last && !isCurrent);
          return (
            <motion.li
              key={m.key}
              className={`stage-chip${isCurrent ? ' stage-chip--live' : ''}`}
              data-stage={m.key}
              data-state={done ? 'done' : isCurrent ? 'live' : 'pending'}
              initial={reduced ? false : { opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: reduced ? 0 : 0.25, delay: reduced ? 0 : i * 0.04 }}
            >
              <span className="stage-chip__mark" aria-hidden>
                {done ? '✓' : isCurrent ? '◐' : '○'}
              </span>
              <span className="stage-chip__label">
                {m.label || STAGE_LABELS[m.key]}
              </span>
              {i > 0 && (
                <span className="stage-chip__dur" data-testid="stage-dur">
                  {isCurrent && !done ? `(${fmt(live)}…)` : fmt(dur)}
                </span>
              )}
            </motion.li>
          );
        })}
        {executing && totalCount > 0 && (
          <motion.li
            className="stage-chip stage-chip--exec"
            data-stage="exec"
            initial={reduced ? false : { opacity: 0, x: -8 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: reduced ? 0 : 0.25 }}
          >
            <span className="stage-chip__mark" aria-hidden>◐</span>
            <span className="stage-chip__label">
              执行 {verifiedCount}/{totalCount} 步
            </span>
            <span className="stage-chip__dur" data-testid="stage-dur">
              ({fmt(now - (marks[marks.length - 1]?.at ?? now))}…)
            </span>
          </motion.li>
        )}
      </ol>
    </section>
  );
}
