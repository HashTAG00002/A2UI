/**
 * CelebrationLayer — the A7 celebration gate (canvas-confetti@1.9.4).
 *
 * The gate is CLOSED (test-pinned in __tests__/governanceEvents.test.ts
 * and here at the component level): only a `final_pass` celebration fires
 * the full-screen 🎉; only a `checkpoint_reward` fires the small burst.
 * Conflict / verification failure / rollback / pause NEVER fire — the
 * negative cases are locked by tests, not by luck.
 *
 * `prefers-reduced-motion` (A7): NO confetti at all — the state change
 * lands instantly; the final banner renders statically. The same rule
 * the ProgressiveTaskPlane set as precedent, extended to celebration.
 *
 * (useReducedMotionConfig is the hook that honors BOTH the OS media
 * query and MotionConfig's override — verified against framer-motion's
 * dist source: "never"→false, "always"→true, else the media query.)
 */
import { useEffect, useRef } from 'react';
import { AnimatePresence, motion, useReducedMotionConfig } from 'motion/react';
import confetti from 'canvas-confetti';
import type { CelebrationState } from '../a2ui/governanceEvents';

const FINAL_COLORS = ['#4f46e5', '#16a34a', '#d97706', '#e11d48'];
const REWARD_COLORS = ['#16a34a', '#4f46e5'];

/** the full-screen finale — only ever called for final_pass */
export function fireFinalCelebration(): void {
  confetti({
    particleCount: 90,
    spread: 100,
    origin: { y: 0.6 },
    colors: FINAL_COLORS,
  });
  window.setTimeout(() => {
    confetti({
      particleCount: 55,
      angle: 60,
      spread: 60,
      origin: { x: 0, y: 0.7 },
      colors: FINAL_COLORS,
    });
  }, 250);
  window.setTimeout(() => {
    confetti({
      particleCount: 55,
      angle: 120,
      spread: 60,
      origin: { x: 1, y: 0.7 },
      colors: FINAL_COLORS,
    });
  }, 450);
}

/** the small checkpoint reward — only ever called for checkpoint_reached */
export function fireCheckpointReward(): void {
  confetti({
    particleCount: 26,
    spread: 55,
    startVelocity: 26,
    scalar: 0.8,
    origin: { x: 0.5, y: 0.3 },
    colors: REWARD_COLORS,
  });
}

export interface CelebrationLayerProps {
  celebration: CelebrationState;
}

export function CelebrationLayer({ celebration }: CelebrationLayerProps) {
  const reduced = useReducedMotionConfig() ?? false;
  const lastNonce = useRef(0);

  useEffect(() => {
    if (celebration.nonce === lastNonce.current) return;
    lastNonce.current = celebration.nonce;
    // A7: reduced motion → no confetti, ever — the state change itself
    // (banner / reached chips) carries the meaning
    if (reduced) return;
    if (celebration.kind === 'final_pass') fireFinalCelebration();
    else if (celebration.kind === 'checkpoint_reward') {
      fireCheckpointReward();
    }
  }, [celebration, reduced]);

  return (
    <AnimatePresence>
      {celebration.kind === 'final_pass' && (
        <motion.div
          key="final-celebration"
          className="final-celebration"
          data-testid="final-celebration"
          data-label={celebration.label}
          data-reduced={reduced ? 'true' : 'false'}
          role="status"
          initial={reduced ? false : { opacity: 0, scale: 0.82 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0 }}
          transition={
            reduced
              ? { duration: 0 }
              : { type: 'spring', stiffness: 300, damping: 24 }
          }
        >
          <span className="final-celebration__emoji" aria-hidden>
            🎉
          </span>
          <p className="final-celebration__title">验证通过 · 任务完成</p>
          {celebration.label && (
            <p className="final-celebration__label">{celebration.label}</p>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
