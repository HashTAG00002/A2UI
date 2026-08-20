/**
 * StageTimeline — the A9.1 staged progress contract:
 *
 *  - every chip comes from a REAL signal mark (parent-stamped);
 *  - completed stages show measured durations; the open stage shows a
 *    LIVE ticking timer;
 *  - the execution chip counts verified/total from kernel truth only.
 */
import { render, screen } from '@testing-library/react';
import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { StageTimeline, type StageMark } from '../progressive/StageTimeline';

const T0 = 1_700_000_000_000;

function marks(): StageMark[] {
  return [
    { key: 'goal', at: T0, label: '接收任务' },
    { key: 't1', at: T0 + 3_200, label: '' },
    { key: 't2', at: T0 + 3_200 + 8_600, label: '' },
  ];
}

describe('StageTimeline — signal-driven staged progress', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0 + 3_200 + 8_600 + 1_000);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing without marks (no fabricated stages)', () => {
    const { container } = render(<StageTimeline marks={[]} />);
    expect(container.querySelector('[data-testid="stage-timeline"]'))
      .toBeNull();
  });

  it('completed stages show their measured durations (✓ 3.2s / ✓ 8.6s)', () => {
    // ready closed the chain: goal/t1/t2 are all DONE, each with the
    // duration measured from the previous signal's arrival
    render(<StageTimeline marks={[
      ...marks(), { key: 'ready', at: T0 + 3_200 + 8_600 + 1_200, label: '' },
    ]} />);
    const chips = document.querySelectorAll('[data-stage]');
    expect(chips.length).toBe(4);
    const durations = screen.getAllByTestId('stage-dur').map((d) => d.textContent);
    expect(durations).toContain('3.2s');
    expect(durations).toContain('8.6s');
    expect(durations).toContain('1.2s');
  });

  it('the last open stage (t2 still compiling) shows a LIVE timer, not a fake duration', () => {
    render(<StageTimeline marks={marks()} />);
    const last = document.querySelector('[data-stage="t2"]');
    expect(last?.getAttribute('data-state')).toBe('live');
    const durations = screen.getAllByTestId('stage-dur').map((d) => d.textContent);
    expect(durations).toEqual(['3.2s', expect.stringMatching(/^\(\d+\.\ds…\)$/)]);
  });

  it('the open stage shows a LIVE timer that ticks every second', () => {
    render(<StageTimeline marks={marks()} />);
    // beforeEach set the clock 1s past t2 — the open chip shows 1.0s…
    expect(screen.getByText(/1\.0s…/)).toBeInTheDocument();
    // move the clock +2s and let TWO interval ticks fire: the chip
    // re-renders with the moved clock — it is ALIVE, not a snapshot
    act(() => {
      vi.setSystemTime(T0 + 3_200 + 8_600 + 3_000);
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.getByText(/5\.0s…/)).toBeInTheDocument();
  });

  it('executing shows the verified/total counter from kernel truth', () => {
    vi.setSystemTime(T0 + 30_000);
    render(
      <StageTimeline marks={marks()} executing
        verifiedCount={2} totalCount={5} />,
    );
    expect(screen.getByText(/执行 2\/5 步/)).toBeInTheDocument();
  });

  it('no executing chip without node truth (totalCount 0)', () => {
    render(<StageTimeline marks={marks()} executing />);
    expect(screen.queryByText(/执行 0\/0 步/)).toBeNull();
  });
});
