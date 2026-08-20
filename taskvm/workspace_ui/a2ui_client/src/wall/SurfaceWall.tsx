/**
 * SurfaceWall — the multi-APP live screenshot wall (A9.2).
 *
 *  - one card per world surface (feed: ``GET /api/app/surface_shots``,
 *    server-assembled URLs — the client never builds URLs from
 *    internal ids, repo contract §3);
 *  - the foreground card rides the live-shot channel (thumbnail ≤240px
 *    + content-hash dedup — see `a2ui/liveShot.ts`); background
 *    surfaces reuse the same card shape at heartbeat cadence;
 *  - fan-out/fan-in lane pulses: when workflow nodes execute
 *    concurrently (≥2 open lanes) the wall shows the fan-out state —
 *    "开工中" — driven PURELY by kernel/workflow node status signals
 *    (the same t2/governance chips the snake consumes);
 *  - click a card → the full-resolution shot lazy-loads in a modal
 *    (the ~2 MB PNG is fetched ONLY on explicit user intent);
 *  - zero-loss: an unchanged hash keeps the previous frame; a failed
 *    fetch keeps the stale frame with an honest "同步中" badge.
 */
import { AnimatePresence, motion, useReducedMotion } from 'motion/react';
import { useEffect, useState } from 'react';
import type { WallEntry } from '../a2ui/liveShot';

export interface WallLane {
  label: string;
  kind: 'step' | 'verification' | 'checkpoint' | 'goal';
  status: 'waiting' | 'executing' | 'verified' | 'failed';
}

export interface SurfaceWallProps {
  /** The live foreground entry (null while the very first shot hasn't
   *  landed — the wall then renders its honest empty state). */
  entry: WallEntry | null;
  /** Decoded thumbnail object URL (stable while the hash is unchanged). */
  thumbUrl: string | null;
  /** True while the first frame is still in flight. */
  loading: boolean;
  /** The world's surfaces (name + role) for cards without a shot yet. */
  surfaces: { name: string; role: string }[];
  /** Workflow lanes driving the pulse animation (kernel truth). */
  lanes: WallLane[];
}

function LanePulse({ lanes }: { lanes: WallLane[] }) {
  const reduced = useReducedMotion() ?? false;
  const active = lanes.filter((l) => l.status === 'executing');
  const fanOut = active.length >= 2;
  if (lanes.length === 0) return null;
  return (
    <div
      className={`wall-lanes${fanOut ? ' wall-lanes--fanout' : ''}`}
      data-testid="wall-lanes"
      data-fanout={fanOut ? 'true' : 'false'}
    >
      {active.length > 0 ? (
        <>
          {fanOut && (
            <p className="wall-lanes__fanout" data-testid="fanout-badge">
              fan-out · {active.length} 个分支开工中
            </p>
          )}
          <ul>
            {active.map((l, i) => (
              <motion.li
                key={`${l.label}-${i}`}
                className="wall-lane wall-lane--executing"
                data-lane-status="executing"
                initial={reduced ? false : { opacity: 0.4 }}
                animate={
                  reduced
                    ? { opacity: 1 }
                    : { opacity: [0.45, 1, 0.45] }
                }
                transition={
                  reduced
                    ? { duration: 0 }
                    : { repeat: Infinity, duration: 1.6, delay: i * 0.25 }
                }
              >
                <span className="wall-lane__dot" aria-hidden>●</span>
                <span className="wall-lane__label">{l.label}</span>
                <span className="wall-lane__state">开工中</span>
              </motion.li>
            ))}
          </ul>
        </>
      ) : (
        <ul>
          {lanes.slice(0, 3).map((l, i) => (
            <li
              key={`${l.label}-${i}`}
              className="wall-lane"
              data-lane-status={l.status}
            >
              <span className="wall-lane__dot" aria-hidden>
                {l.status === 'verified' ? '●' : l.status === 'failed' ? '✕' : '○'}
              </span>
              <span className="wall-lane__label">{l.label}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ShotModal({ fullUrl, name, onClose }: {
  fullUrl: string;
  name: string;
  onClose: () => void;
}) {
  const [loaded, setLoaded] = useState(false);
  const reduced = useReducedMotion() ?? false;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <motion.div
      className="shot-modal"
      data-testid="shot-modal"
      role="dialog"
      aria-label={`${name} 当前截图`}
      initial={reduced ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      onClick={onClose}
    >
      <div className="shot-modal__inner" onClick={(e) => e.stopPropagation()}>
        <header className="shot-modal__bar">
          <span className="shot-modal__name">{name} · 当前截图</span>
          <button
            type="button"
            className="gov-btn gov-btn--ghost"
            onClick={onClose}
            data-testid="shot-modal-close"
          >
            关闭
          </button>
        </header>
        {!loaded && <div className="shot-modal__loading">全图加载中…</div>}
        <img
          src={fullUrl}
          alt={`${name} 当前截图`}
          onLoad={() => setLoaded(true)}
          data-testid="shot-modal-img"
        />
      </div>
    </motion.div>
  );
}

export function SurfaceWall({
  entry,
  thumbUrl,
  loading,
  surfaces,
  lanes,
}: SurfaceWallProps) {
  const [openFull, setOpenFull] = useState(false);
  const reduced = useReducedMotion() ?? false;
  const cards = surfaces.length > 0
    ? surfaces.map((s, i) => ({
      name: s.name,
      role: s.role as WallEntry['role'],
      live: i === 0 ? entry : null,
    }))
    : entry
      ? [{ name: entry.name, role: entry.role, live: entry }]
      : [];

  if (cards.length === 0 && loading) {
    return (
      <section className="surface-wall surface-wall--empty"
        data-testid="surface-wall" data-empty="true">
        <p className="surface-wall__hint">截图同步中…</p>
      </section>
    );
  }
  if (cards.length === 0) return null;

  return (
    <section
      className="surface-wall"
      data-testid="surface-wall"
      aria-label="应用截图墙"
    >
      <header className="surface-wall__head">
        <h2 className="surface-wall__title">实时应用</h2>
        <span className="surface-wall__count">{cards.length}</span>
      </header>
      <div className="surface-wall__grid">
        {cards.map((c) => (
          <motion.article
            key={c.name}
            className="surface-card"
            data-testid="surface-card"
            data-role={c.role}
            data-sync={loading && c.live === null ? 'true' : 'false'}
            whileHover={reduced ? undefined : { y: -3 }}
            onClick={() => c.live && setOpenFull(true)}
          >
            <div className="surface-card__shot">
              {c.live && thumbUrl ? (
                <img
                  src={thumbUrl}
                  alt={`${c.name} 实时缩略图`}
                  data-testid="surface-thumb"
                  data-hash={c.live.hash}
                />
              ) : (
                <div className="surface-card__placeholder">
                  {loading ? '同步中…' : '暂无截图'}
                </div>
              )}
              {c.role === 'foreground' && (
                <span className="surface-card__badge">实时</span>
              )}
            </div>
            <footer className="surface-card__meta">
              <span className="surface-card__name">{c.name}</span>
              <span className="surface-card__hint">点击放大</span>
            </footer>
          </motion.article>
        ))}
      </div>
      <LanePulse lanes={lanes} />
      <AnimatePresence>
        {openFull && entry && (
          <ShotModal
            fullUrl={entry.fullUrl}
            name={entry.name}
            onClose={() => setOpenFull(false)}
          />
        )}
      </AnimatePresence>
    </section>
  );
}
