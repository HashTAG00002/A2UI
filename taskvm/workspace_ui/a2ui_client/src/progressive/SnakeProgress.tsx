/**
 * SnakeProgress — the A7 verified snake trajectory (MASTER_HANDOVER L167:
 * "verified snake progress 只跨 verified milestone").
 *
 * The trajectory is a serpentine path through the workflow nodes (4
 * milestones per row, alternating direction). The PURE crossing rules
 * live in `a2ui/snake.ts` (test-pinned); this component owns the visual
 * honesty:
 *
 *  - the body only advances onto VERIFIED milestones — one milestone per
 *    tick, spring-smoothed, NEVER a fabricated jump;
 *  - an EXECUTING node shows the head poking into the segment toward it
 *    (half a segment at most) with a gentle breathing pulse — the head
 *    never passes an unverified milestone;
 *  - a rollback event plays the trajectory BACKWARDS, milestone by
 *    milestone, to the target checkpoint node;
 *  - `frozen` (paused / stopped) freezes all forward movement — the
 *    status pill keeps pulsing, the snake honestly does not move;
 *  - `prefers-reduced-motion` degrades everything to instant state
 *    switches (no ticks, no springs, no breathing).
 */
import { motion, useReducedMotionConfig } from 'motion/react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { WorkflowNodeChip } from './ProgressiveTaskPlane';
import {
  POKE_MAX,
  rollbackTargetIndex,
  snakeModel,
} from '../a2ui/snake';
import type { RollbackPlayback } from '../a2ui/governanceEvents';

/** grid geometry: 4 milestones per serpentine row */
const PER_ROW = 4;
/** ms per milestone while advancing / playing back */
export const SNAKE_TICK_MS = 500;
/** px height of each serpentine row */
const ROW_HEIGHT = 104;

export interface SnakeProgressProps {
  nodes: WorkflowNodeChip[];
  /** paused: forward progress freezes (never faked) — only the status
   *  pill keeps breathing; the reverse playback still answers explicit
   *  governance rollbacks. A terminal verdict is NOT a freeze: the
   *  trajectory honestly settles onto the final verified state. */
  frozen?: boolean;
  /** an ACTIVE rollback playback (from the governance motion state) */
  playback?: RollbackPlayback | null;
  onRollbackDone?: () => void;
}

/** index → serpentine grid position (col+0.5, row+0.5 in grid units) */
function gridPos(i: number): { x: number; y: number } {
  const row = Math.floor(i / PER_ROW);
  const col = row % 2 === 0 ? i % PER_ROW : PER_ROW - 1 - (i % PER_ROW);
  return { x: col + 0.5, y: row + 0.5 };
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

export function SnakeProgress({
  nodes,
  frozen = false,
  playback = null,
  onRollbackDone,
}: SnakeProgressProps) {
  // useReducedMotionConfig: the OS media query by default, plus
  // MotionConfig's official override seam (verified against
  // framer-motion's dist source — "always"→true, "never"→false)
  const reduced = useReducedMotionConfig() ?? false;
  const model = useMemo(() => snakeModel(nodes), [nodes]);
  const rows = Math.max(1, Math.ceil(nodes.length / PER_ROW));

  const playbackTarget = useMemo(
    () =>
      playback?.active
        ? rollbackTargetIndex(nodes, playback.targetLabel ?? null)
        : null,
    [playback, nodes],
  );
  const playbackActive = playback?.active === true && playbackTarget !== null;
  const effectiveTarget = playbackActive ? (playbackTarget as number) : model.crossed;

  const [visualCrossed, setVisualCrossed] = useState(0);
  const frozenRef = useRef(frozen);
  frozenRef.current = frozen;

  // the stepping ticker: forward only when NOT frozen (pause honesty),
  // reverse always runs (an explicit governance rollback must play back);
  // reduced motion snaps instantly (unless frozen — frozen means HOLD).
  useEffect(() => {
    if (reduced) {
      if (!frozen) setVisualCrossed(effectiveTarget);
      return undefined;
    }
    const id = setInterval(() => {
      setVisualCrossed((c) => {
        if (c === effectiveTarget) return c;
        if (c < effectiveTarget) return frozenRef.current ? c : c + 1;
        return c - 1;
      });
    }, SNAKE_TICK_MS);
    return () => clearInterval(id);
  }, [effectiveTarget, reduced, frozen]);

  // clamp when the node list itself shrank (new goal reset). Note
  // `crossed` COUNTS milestones: all-verified means crossed === N.
  useEffect(() => {
    setVisualCrossed((c) => clamp(c, 0, nodes.length));
  }, [nodes.length]);

  // playback completion — once per rollback nonce. A rollback whose
  // label matches no node honestly completes as a no-op (the server's
  // next t2 node statuses settle the state; nothing is faked).
  const doneNonce = useRef(-1);
  useEffect(() => {
    if (!playback?.active || doneNonce.current === playback.nonce) return;
    if (playbackTarget === null || visualCrossed === playbackTarget) {
      doneNonce.current = playback.nonce;
      onRollbackDone?.();
    }
  }, [playback, playbackTarget, visualCrossed, onRollbackDone]);

  const poking =
    model.pokeIndex !== null &&
    visualCrossed === model.crossed &&
    !playback?.active;
  const headUnits =
    nodes.length === 0
      ? 0
      : clamp(visualCrossed + (poking ? POKE_MAX : 0), 0, nodes.length - 1);

  // head position: piecewise-linear interpolation along the serpentine
  const i0 = clamp(Math.floor(headUnits), 0, nodes.length - 1);
  const i1 = clamp(Math.ceil(headUnits), 0, nodes.length - 1);
  const f = headUnits - i0;
  const a = gridPos(i0);
  const b = gridPos(i1);
  const headLeft = ((a.x + (b.x - a.x) * f) / PER_ROW) * 100;
  const headTop = ((a.y + (b.y - a.y) * f) / rows) * 100;

  const pathD = useMemo(
    () =>
      nodes.length < 2
        ? ''
        : nodes
            .map((_, i) => {
              const p = gridPos(i);
              return `${i === 0 ? 'M' : 'L'}${p.x} ${p.y}`;
            })
            .join(' '),
    [nodes],
  );
  const bodyFraction =
    nodes.length < 2 ? 1 : headUnits / (nodes.length - 1);

  if (nodes.length === 0) return null;

  return (
    <div
      className="snake"
      data-testid="snake-progress"
      data-crossed={visualCrossed}
      data-model-crossed={model.crossed}
      data-poking={poking ? 'true' : 'false'}
      data-frozen={frozen ? 'true' : 'false'}
      data-rollback-active={playbackActive ? 'true' : 'false'}
      data-nodes={nodes.length}
      style={{ height: `${rows * ROW_HEIGHT}px` }}
    >
      <svg
        className="snake__svg"
        viewBox={`0 0 ${PER_ROW} ${rows}`}
        preserveAspectRatio="none"
        aria-hidden
      >
        {pathD && (
          <path
            className="snake__track"
            d={pathD}
            fill="none"
            strokeWidth={6}
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {pathD && (
          <motion.path
            className="snake__body"
            d={pathD}
            fill="none"
            strokeWidth={6}
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
            initial={false}
            animate={{ pathLength: bodyFraction }}
            transition={
              reduced
                ? { duration: 0 }
                : { type: 'spring', stiffness: 120, damping: 22 }
            }
          />
        )}
      </svg>

      {nodes.map((n, i) => {
        const p = gridPos(i);
        const crossed = i < visualCrossed;
        const cls = crossed
          ? 'snake__node snake__node--crossed'
          : `snake__node snake__node--${n.status}`;
        return (
          <div
            key={`${n.label}-${i}`}
            className={`${cls} snake__node--k-${n.kind}`}
            data-testid="snake-node"
            data-index={i}
            data-status={n.status}
            data-crossed={crossed ? 'true' : 'false'}
            style={{
              left: `${(p.x / PER_ROW) * 100}%`,
              top: `${(p.y / rows) * 100}%`,
            }}
          >
            <span className="snake__dot" aria-hidden>
              {n.status === 'failed' ? '✕' : ''}
            </span>
            <span className="snake__label">{n.label}</span>
            <span className="snake__kind">{n.kind}</span>
          </div>
        );
      })}

      <motion.div
        className={`snake__head${poking ? ' snake__head--poking' : ''}`}
        data-testid="snake-head"
        data-poking={poking ? 'true' : 'false'}
        initial={false}
        animate={{ left: `${headLeft}%`, top: `${headTop}%` }}
        transition={
          reduced
            ? { duration: 0 }
            : { type: 'spring', stiffness: 200, damping: 24 }
        }
      >
        <motion.span
          className="snake__head-core"
          aria-hidden
          animate={
            poking && !frozen && !reduced
              ? { scale: [1, 1.35, 1] }
              : { scale: 1 }
          }
          transition={
            poking && !frozen && !reduced
              ? { repeat: Infinity, duration: 1.2, ease: 'easeInOut' }
              : { duration: 0.2 }
          }
        />
      </motion.div>

      {playbackActive && (
        <p className="snake__caption" data-testid="snake-rollback-caption">
          回放中 → {playback?.targetLabel}
        </p>
      )}
      {frozen && !playbackActive && (
        <p
          className="snake__caption snake__caption--paused"
          data-testid="snake-paused-caption"
        >
          已暂停 · 进度冻结
        </p>
      )}
    </div>
  );
}
