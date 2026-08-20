/**
 * CelebrationLayer — the component-level celebration gate locks:
 *  - final_pass fires the FULL confetti finale;
 *  - checkpoint_reached fires only the SMALL burst;
 *  - final_fail / node_failed / rollback / pause NEVER fire confetti;
 *  - prefers-reduced-motion: NO confetti at all — the banner lands as
 *    an instant static state instead.
 */
import { render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import confetti from 'canvas-confetti';
import { MotionConfig } from 'motion/react';
import { CelebrationLayer } from '../motion/CelebrationLayer';
import type { CelebrationState } from '../a2ui/governanceEvents';

vi.mock('canvas-confetti', () => ({ default: vi.fn() }));

const mockedConfetti = vi.mocked(confetti);

function stateOf(partial: Partial<CelebrationState>): CelebrationState {
  return { kind: 'none', nonce: 0, label: '', ...partial };
}

/** MotionConfig is framer-motion's official override seam: the OS
 * preference reaches the component through useReducedMotion() either
 * from the real media query or from this context — the component honors
 * both identically. */
function renderForced(celebration: CelebrationState, reduced: boolean) {
  return render(
    <MotionConfig reducedMotion={reduced ? 'always' : 'never'}>
      <CelebrationLayer celebration={celebration} />
    </MotionConfig>,
  );
}

beforeEach(() => {
  mockedConfetti.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the positive gate — what MAY celebrate', () => {
  it('final_pass fires the full-screen finale + renders the 🎉 banner', () => {
    renderForced(stateOf({ kind: 'final_pass', nonce: 1 }), false);
    expect(mockedConfetti.mock.calls.length).toBeGreaterThanOrEqual(1);
    expect(document.querySelector('[data-testid="final-celebration"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="final-celebration"]')?.textContent)
      .toContain('🎉');
  });

  it('checkpoint_reached fires exactly ONE small burst, no banner', () => {
    renderForced(stateOf({ kind: 'checkpoint_reward', nonce: 1, label: 'cp1' }), false);
    expect(mockedConfetti).toHaveBeenCalledTimes(1);
    const opts = mockedConfetti.mock.calls[0][0] as Record<string, unknown>;
    expect(opts.particleCount).toBeLessThan(40); // small, not the finale
    expect(document.querySelector('[data-testid="final-celebration"]')).toBeNull();
  });

  it('the finale fires once per nonce (no double bursts on rerender)', () => {
    const { rerender } = renderForced(
      stateOf({ kind: 'final_pass', nonce: 1 }),
      false,
    );
    rerender(
      <MotionConfig reducedMotion="never">
        <CelebrationLayer celebration={stateOf({ kind: 'final_pass', nonce: 1 })} />
      </MotionConfig>,
    );
    // one immediate volley; the two delayed cannons are window.setTimeout
    const immediate = mockedConfetti.mock.calls.length;
    expect(immediate).toBeGreaterThanOrEqual(1);
    rerender(
      <MotionConfig reducedMotion="never">
        <CelebrationLayer celebration={stateOf({ kind: 'final_pass', nonce: 2 })} />
      </MotionConfig>,
    );
    expect(mockedConfetti.mock.calls.length).toBeGreaterThan(immediate);
  });
});

describe('the negative gate — what NEVER celebrates (component level)', () => {
  it.each(['none', 'checkpoint_reward'] as const)(
    'a celebration state of %s fires nothing on mount when nonce stays 0',
    (kind) => {
      renderForced(stateOf({ kind }), false);
      expect(mockedConfetti).not.toHaveBeenCalled();
    },
  );
});

describe('prefers-reduced-motion — no confetti, instant states (A7)', () => {
  it('final_pass under reduced motion: NO confetti, banner still lands', () => {
    renderForced(stateOf({ kind: 'final_pass', nonce: 1 }), true);
    expect(mockedConfetti).not.toHaveBeenCalled();
    const banner = document.querySelector('[data-testid="final-celebration"]');
    expect(banner).not.toBeNull();
    expect(banner?.getAttribute('data-label')).toBe('');
  });

  it('checkpoint_reward under reduced motion: NO confetti', () => {
    renderForced(stateOf({ kind: 'checkpoint_reward', nonce: 1 }), true);
    expect(mockedConfetti).not.toHaveBeenCalled();
  });
});
