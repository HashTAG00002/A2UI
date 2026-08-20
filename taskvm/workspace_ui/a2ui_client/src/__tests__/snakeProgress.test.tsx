/**
 * SnakeProgress — the visual honesty locks (fake-timer driven):
 *  - the trajectory advances one verified milestone per tick — never a
 *    fabricated jump;
 *  - an executing node pokes the head WITHOUT crossing its milestone;
 *  - frozen (pause/stop) holds the visual position while server truth
 *    keeps arriving; unfreezing catches up;
 *  - a rollback plays the trajectory BACKWARDS to the target checkpoint,
 *    then reports completion exactly once;
 *  - prefers-reduced-motion degrades to instant state switches;
 *  - empty node lists render nothing.
 */
import { act, render } from '@testing-library/react';
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { MotionConfig } from 'motion/react';
import {
  SnakeProgress,
  SNAKE_TICK_MS,
} from '../progressive/SnakeProgress';
import type { WorkflowNodeChip } from '../progressive/ProgressiveTaskPlane';

function nodes(...statuses: WorkflowNodeChip['status'][]): WorkflowNodeChip[] {
  return statuses.map((status, i) => ({
    label: `节点${i + 1}`,
    kind: i === 1 ? 'checkpoint' : 'step',
    status,
  }));
}

const TICK = SNAKE_TICK_MS;

function root() {
  return document.querySelector('[data-testid="snake-progress"]')!;
}

function crossed() {
  return Number(root().getAttribute('data-crossed'));
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function advance(ticks: number) {
  act(() => {
    vi.advanceTimersByTime(TICK * ticks);
  });
}

describe('verified-only advancement', () => {
  it('advances one milestone per tick onto verified nodes only', () => {
    render(<SnakeProgress nodes={nodes('verified', 'verified', 'verified')} />);
    expect(crossed()).toBe(0);
    advance(1);
    expect(crossed()).toBe(1);
    advance(1);
    expect(crossed()).toBe(2);
    advance(1);
    expect(crossed()).toBe(3); // all-verified: crossed === node count
    advance(2);
    expect(crossed()).toBe(3); // never overshoots
  });

  it('stops at the unverified boundary — waiting nodes are never crossed', () => {
    render(<SnakeProgress nodes={nodes('verified', 'waiting', 'verified')} />);
    advance(10);
    expect(crossed()).toBe(1);
    // node3 IS verified, but sits behind waiting node2 — the snake never
    // skips to it; its own chip still shows the server status
    const node3 = document.querySelector('[data-index="2"]')!;
    expect(node3.getAttribute('data-status')).toBe('verified');
    expect(node3.getAttribute('data-crossed')).toBe('false');
  });

  it('marks exactly the crossed prefix on the node chips', () => {
    render(<SnakeProgress nodes={nodes('verified', 'verified', 'waiting')} />);
    advance(10);
    expect(document.querySelector('[data-index="0"]')!.getAttribute('data-crossed'))
      .toBe('true');
    expect(document.querySelector('[data-index="1"]')!.getAttribute('data-crossed'))
      .toBe('true');
    expect(document.querySelector('[data-index="2"]')!.getAttribute('data-crossed'))
      .toBe('false');
  });
});

describe('the executing poke — 探出头但不越界', () => {
  it('shows the head poking toward an executing node without crossing it', () => {
    render(<SnakeProgress nodes={nodes('verified', 'executing', 'waiting')} />);
    advance(1); // arrive at milestone 1
    expect(root().getAttribute('data-poking')).toBe('true');
    expect(crossed()).toBe(1); // the poke NEVER increments crossings
    const head = document.querySelector('[data-testid="snake-head"]')!;
    expect(head.getAttribute('data-poking')).toBe('true');
  });

  it('waiting nodes never poke', () => {
    render(<SnakeProgress nodes={nodes('verified', 'waiting')} />);
    advance(1);
    expect(root().getAttribute('data-poking')).toBe('false');
  });

  it('failed nodes never poke and show the ✕ mark', () => {
    render(<SnakeProgress nodes={nodes('verified', 'failed')} />);
    advance(1);
    expect(root().getAttribute('data-poking')).toBe('false');
    expect(document.querySelector('[data-index="1"]')!.getAttribute('data-status'))
      .toBe('failed');
  });
});

describe('pause honesty — frozen means frozen', () => {
  it('holds the visual position while frozen, then catches up on resume', () => {
    const { rerender } = render(
      <SnakeProgress nodes={nodes('verified', 'verified', 'verified')} frozen />,
    );
    advance(5);
    expect(crossed()).toBe(0); // nothing moved while frozen
    expect(root().getAttribute('data-frozen')).toBe('true');

    // server truth keeps arriving while paused — the visual MUST NOT move
    rerender(
      <SnakeProgress
        nodes={nodes('verified', 'verified', 'verified', 'verified')}
        frozen
      />,
    );
    advance(5);
    expect(crossed()).toBe(0);

    // resume: catch up to the current verified prefix
    rerender(
      <SnakeProgress nodes={nodes('verified', 'verified', 'verified', 'verified')} />,
    );
    advance(4);
    expect(crossed()).toBe(4);
  });

  it('shows the frozen caption while paused', () => {
    render(<SnakeProgress nodes={nodes('verified')} frozen />);
    expect(screen_query('snake-paused-caption')).not.toBeNull();
  });

  it('an explicit rollback playback still runs while frozen', () => {
    const { rerender } = render(
      <SnakeProgress nodes={nodes('verified', 'verified', 'verified', 'verified')} />,
    );
    advance(3);
    expect(crossed()).toBe(3);
    rerender(
      <SnakeProgress
        nodes={nodes('verified', 'verified', 'verified', 'verified')}
        frozen
        playback={{ active: true, targetLabel: '节点1', nonce: 1 }}
      />,
    );
    advance(2);
    expect(crossed()).toBe(1); // reverse playback answers the governance call
  });
});

describe('rollback reverse playback', () => {
  it('plays BACKWARDS to the target checkpoint, then completes exactly once', () => {
    const onRollbackDone = vi.fn();
    const all = nodes('verified', 'verified', 'verified', 'verified', 'verified');
    const { rerender } = render(<SnakeProgress nodes={all} />);
    advance(4);
    expect(crossed()).toBe(4);

    rerender(
      <SnakeProgress
        nodes={all}
        playback={{ active: true, targetLabel: '节点2', nonce: 1 }}
        onRollbackDone={onRollbackDone}
      />,    
    );
    expect(root().getAttribute('data-rollback-active')).toBe('true');
    expect(screen_query('snake-rollback-caption')?.textContent).toContain('节点2');

    advance(1);
    expect(crossed()).toBe(3);
    expect(onRollbackDone).not.toHaveBeenCalled();
    advance(2);
    expect(crossed()).toBe(1);
    expect(onRollbackDone).toHaveBeenCalledTimes(1);
    // stays at the target — no rebound, no re-fire
    advance(3);
    expect(crossed()).toBe(1);
    expect(onRollbackDone).toHaveBeenCalledTimes(1);
  });

  it('a rollback whose label matches no node completes as an honest no-op', () => {
    const onRollbackDone = vi.fn();
    render(
      <SnakeProgress
        nodes={nodes('verified')}
        playback={{ active: true, targetLabel: '不存在的节点', nonce: 1 }}
        onRollbackDone={onRollbackDone}
      />,
    );
    expect(onRollbackDone).toHaveBeenCalledTimes(1);
    expect(root().getAttribute('data-rollback-active')).toBe('false');
  });
});

describe('prefers-reduced-motion — instant state switches', () => {
  /** MotionConfig is framer-motion's official override seam — the same
   * useReducedMotion() the OS media query drives in production. */
  function renderReduced(props: Parameters<typeof SnakeProgress>[0]) {
    return render(
      <MotionConfig reducedMotion="always">
        <SnakeProgress {...props} />
      </MotionConfig>,
    );
  }

  it('snaps instantly to the verified prefix — no ticking', () => {
    renderReduced({ nodes: nodes('verified', 'verified', 'waiting') });
    expect(crossed()).toBe(2); // immediately, zero ticks
    advance(5);
    expect(crossed()).toBe(2);
  });

  it('rollback under reduced motion lands instantly at the target', () => {
    renderReduced({
      nodes: nodes('verified', 'verified', 'verified'),
      playback: { active: true, targetLabel: '节点2', nonce: 1 },
    });
    expect(crossed()).toBe(1); // the reverse playback is a single frame
  });
});

describe('empty honesty', () => {
  it('renders nothing without nodes', () => {
    const { container } = render(<SnakeProgress nodes={[]} />);
    expect(container.querySelector('[data-testid="snake-progress"]')).toBeNull();
  });
});

function screen_query(testid: string): HTMLElement | null {
  return document.querySelector(`[data-testid="${testid}"]`);
}
