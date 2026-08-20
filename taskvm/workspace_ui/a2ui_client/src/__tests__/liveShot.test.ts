/**
 * liveShot — the A9.1 thumbnail pipeline contract:
 *
 *  - hash dedup: an unchanged feed hash ⇒ ZERO image fetches;
 *  - 150ms burst coalescing: bursts commit ONE image swap, last wins;
 *  - slow-network adaptation: the poll interval backs off with the
 *    measured RTT (EMA), capped; fast links stay at base cadence.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  BASE_POLL_MS,
  COALESCE_MS,
  MAX_POLL_MS,
  computeNextPollMs,
  createCoalescer,
  isUnchanged,
  nextEma,
  type WallEntry,
} from '../a2ui/liveShot';

function entry(hash: string): WallEntry {
  return {
    name: '微信', role: 'foreground', hash, seq: 1,
    thumbUrl: `/api/app/screenshot?thumb=1&w=240&h=${hash}`,
    fullUrl: `/api/app/screenshot?h=${hash}`,
  };
}

describe('computeNextPollMs — adaptive cadence', () => {
  it('fast links stay at base cadence', () => {
    expect(computeNextPollMs(120)).toBe(BASE_POLL_MS);
    expect(computeNextPollMs(0)).toBe(BASE_POLL_MS);
  });

  it('slow links back off proportionally to the EMA RTT', () => {
    const slow = computeNextPollMs(2_100);   // 2.1s RTT ≈ 3× base
    expect(slow).toBeGreaterThan(BASE_POLL_MS);
    expect(slow).toBeLessThanOrEqual(MAX_POLL_MS);
  });

  it('the backoff is capped (never longer than MAX_POLL_MS)', () => {
    expect(computeNextPollMs(60_000)).toBe(MAX_POLL_MS);
  });
});

describe('nextEma — smoothing', () => {
  it('first sample seeds; later samples smooth (α = 0.3)', () => {
    expect(nextEma(0, 900)).toBe(900);
    expect(nextEma(900, 300)).toBe(Math.round(900 * 0.7 + 300 * 0.3));
  });
});

describe('isUnchanged — hash dedup', () => {
  it('same hash ⇒ unchanged (skip the image fetch entirely)', () => {
    expect(isUnchanged(entry('abc'), entry('abc'))).toBe(true);
  });
  it('a new hash ⇒ changed', () => {
    expect(isUnchanged(entry('abc'), entry('def'))).toBe(false);
  });
  it('null on either side ⇒ changed (first frame must fetch)', () => {
    expect(isUnchanged(null, entry('abc'))).toBe(false);
    expect(isUnchanged(entry('abc'), null)).toBe(false);
  });
});

describe('createCoalescer — 150ms burst merging', () => {
  afterEach(() => vi.useRealTimers());

  it('a burst schedules ONE commit; the LAST payload wins', () => {
    vi.useFakeTimers();
    const co = createCoalescer<number>();
    const commits: number[] = [];
    co.schedule(1, (v) => commits.push(v));
    co.schedule(2, (v) => commits.push(v));
    co.schedule(3, (v) => commits.push(v));
    expect(commits).toEqual([]);      // nothing before the window closes
    vi.advanceTimersByTime(COALESCE_MS + 5);
    expect(commits).toEqual([3]);     // ONE commit, last payload
  });

  it('a second burst after the window commits again', () => {
    vi.useFakeTimers();
    const co = createCoalescer<number>();
    const commits: number[] = [];
    co.schedule(1, (v) => commits.push(v));
    vi.advanceTimersByTime(COALESCE_MS + 5);
    co.schedule(2, (v) => commits.push(v));
    vi.advanceTimersByTime(COALESCE_MS + 5);
    expect(commits).toEqual([1, 2]);
  });

  it('cancel drops the pending payload', () => {
    vi.useFakeTimers();
    const co = createCoalescer<number>();
    const commits: number[] = [];
    co.schedule(7, (v) => commits.push(v));
    co.cancel();
    vi.advanceTimersByTime(COALESCE_MS * 3);
    expect(commits).toEqual([]);
  });
});
