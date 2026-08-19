/**
 * ProgressiveTaskPlane — the progressive task surface skeleton
 * (workplan §20.1): the user sees "it already started" the instant the
 * goal is submitted, and the skeleton morphs level by level as the
 * compile chain returns — WITHOUT ever faking plan content.
 *
 *   T0  goal submit instant → single pulsing workflow node
 *       ("正在编译任务世界…") — pure local render, <100ms
 *   T1  compiler returns    → variable skeletons land (labels known,
 *       values are pending placeholders — NEVER fabricated values)
 *   T2  architect returns   → the single dot morphs into the real DAG
 *       (nodes land one by one; verify/checkpoint kinds are tinted)
 *   live decoder returns    → skeleton is replaced by the real A2UI
 *       component tree (handled by the parent, not this component)
 *
 * Honesty rules (test-pinned):
 *  - T0/T1 show ZERO variable values — only labels + pending marks;
 *  - T2 shows only nodes the architect actually returned;
 *  - `prefers-reduced-motion` disables all entrance animation.
 */
import { AnimatePresence, motion, useReducedMotion } from 'motion/react';
import type { ReactNode } from 'react';

export type CompilePhase = 't0' | 't1' | 't2' | 'live';

export interface VariableSkeleton {
  /** display_label only — no values before the world is compiled. */
  label: string;
}

export type NodeKind = 'step' | 'verification' | 'checkpoint' | 'goal';
export type NodeStatusChip = 'waiting' | 'executing' | 'verified' | 'failed';

export interface WorkflowNodeChip {
  label: string;
  kind: NodeKind;
  status: NodeStatusChip;
}

export interface ProgressiveTaskPlaneProps {
  phase: CompilePhase;
  variableSkeletons: VariableSkeleton[];
  workflowNodes: WorkflowNodeChip[];
  /** Rendered when phase === 'live' (the A2UI surface). */
  live?: ReactNode;
}

const KIND_TONES: Record<NodeKind, string> = {
  step: 'wf-node wf-node--step',
  verification: 'wf-node wf-node--verification',
  checkpoint: 'wf-node wf-node--checkpoint',
  goal: 'wf-node wf-node--goal',
};

const STATUS_DOTS: Record<NodeStatusChip, string> = {
  waiting: '○',
  executing: '◐',
  verified: '●',
  failed: '✕',
};

function spring(reduced: boolean) {
  return reduced
    ? { duration: 0 }
    : { type: 'spring' as const, stiffness: 420, damping: 30, mass: 0.7 };
}

export function ProgressiveTaskPlane({
  phase,
  variableSkeletons,
  workflowNodes,
  live,
}: ProgressiveTaskPlaneProps) {
  const reduced = useReducedMotion() ?? false;

  if (phase === 'live') {
    return (
      <div className="plane plane--live" data-testid="plane-live">{live}</div>
    );
  }

  return (
    <div className="plane" data-testid={`plane-${phase}`}>
      <AnimatePresence mode="popLayout" initial={!reduced}>
        {/* ── T0+: the pulsing "compiling" node (always honest: one dot) ── */}
        {phase === 't0' && (
          <motion.section
            key="t0-pulse"
            className="plane__compile"
            data-testid="t0-pulse"
            layout
            initial={{ opacity: 0, scale: 0.94 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.94 }}
            transition={spring(reduced)}
          >
            <span className="pulse-node" aria-hidden />
            <p className="plane__caption">正在编译任务世界…</p>
            <p className="plane__hint">编译完成前不会开始执行</p>
          </motion.section>
        )}

        {/* ── T1+: variable skeletons (labels only, values pending) ── */}
        {phase !== 't0' && (
          <motion.section
            key="t1-vars"
            className="plane__vars"
            data-testid="t1-vars"
            layout
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={spring(reduced)}
          >
            <h2 className="plane__section-title">任务变量</h2>
            <ul className="var-skeleton-list">
              {variableSkeletons.map((v, i) => (
                <motion.li
                  key={v.label}
                  className="var-skeleton"
                  data-testid="var-skeleton"
                  initial={reduced ? false : { opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ ...spring(reduced), delay: reduced ? 0 : i * 0.05 }}
                >
                  <span className="var-skeleton__label">{v.label}</span>
                  <span className="var-skeleton__pending" aria-label="值待编译">
                    ···
                  </span>
                </motion.li>
              ))}
            </ul>
          </motion.section>
        )}

        {/* ── T2: the dot morphs into the real DAG ── */}
        {phase === 't2' && workflowNodes.length > 0 && (
          <motion.section
            key="t2-dag"
            className="plane__dag"
            data-testid="t2-dag"
            layout
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={spring(reduced)}
          >
            <h2 className="plane__section-title">计划</h2>
            <ol className="dag-list">
              {workflowNodes.map((n, i) => (
                <motion.li
                  key={`${n.label}-${i}`}
                  className={KIND_TONES[n.kind]}
                  data-kind={n.kind}
                  initial={reduced ? false : { opacity: 0, y: 10, scale: 0.96 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  transition={{ ...spring(reduced), delay: reduced ? 0 : 0.08 + i * 0.07 }}
                >
                  <span className="wf-node__dot" aria-hidden>
                    {STATUS_DOTS[n.status]}
                  </span>
                  <span className="wf-node__label">{n.label}</span>
                  <span className="wf-node__kind">{n.kind}</span>
                </motion.li>
              ))}
            </ol>
          </motion.section>
        )}
      </AnimatePresence>
    </div>
  );
}
