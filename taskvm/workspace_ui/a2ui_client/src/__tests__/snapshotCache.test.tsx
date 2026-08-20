/**
 * snapshotCache + SWR first-render — the A9.1 contract "entering a
 * session NEVER shows a blank loading page":
 *
 *  1. a saved snapshot hydrates the island IMMEDIATELY (goal text,
 *     phase, skeletons) with the 同步中 badge on;
 *  2. the FIRST live server signal (progress / ordered a2ui message /
 *    governance event) retires the badge — server truth replaces cache;
 *  3. the cache stores screen-visible fields ONLY (labels, no values,
 *     no internal ids).
 */
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TaskExperience } from '../TaskExperience';
import {
  clearSnapshot,
  loadSnapshot,
  saveSnapshot,
} from '../a2ui/snapshotCache';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, Array<(ev: { data: string }) => void>>();
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, fn: (ev: { data: string }) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  emit(type: string, payload: unknown) {
    const ev = { data: JSON.stringify(payload) };
    if (type === 'message') this.onmessage?.(ev);
    else this.listeners.get(type)?.forEach((fn) => fn(ev));
  }
  close() { /* noop */ }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  localStorage.clear();
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource);
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => ({ ok: true, goals: [] }),
    blob: async () => new Blob(['x']),
  }) as unknown as Response));
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('snapshotCache — pure storage contract', () => {
  it('round-trips the visible fields', () => {
    saveSnapshot({
      goal: '把发布会改到周五', phase: 't1', status: 'compiling',
      skeletons: [{ label: '发布日期' }],
      nodes: [{ label: '修改日期', kind: 'step', status: 'waiting' }],
      checkpoints: ['检查点 1'],
    });
    const snap = loadSnapshot();
    expect(snap?.goal).toBe('把发布会改到周五');
    expect(snap?.phase).toBe('t1');
    expect(snap?.skeletons).toEqual([{ label: '发布日期' }]);
    expect(snap?.nodes).toHaveLength(1);
  });

  it('never stores variable VALUES — labels only (honesty rule)', () => {
    saveSnapshot({
      goal: 'g', phase: 't1', status: 'compiling',
      skeletons: [{ label: '发布日期' }], nodes: [], checkpoints: [],
    });
    const raw = localStorage.getItem('taskvm.island.snapshot.v1') ?? '';
    expect(raw).not.toContain('desired');
    expect(raw).not.toContain('entity_id');
    expect(raw).not.toContain('value');
  });

  it('clearSnapshot empties the cache; a corrupt payload reads as null', () => {
    saveSnapshot({ goal: 'g', phase: 't0', status: 'compiling',
      skeletons: [], nodes: [], checkpoints: [] });
    clearSnapshot();
    expect(loadSnapshot()).toBeNull();
    localStorage.setItem('taskvm.island.snapshot.v1', '{not json');
    expect(loadSnapshot()).toBeNull();
  });
});

describe('TaskExperience — SWR first render', () => {
  it('hydrates from the last snapshot IMMEDIATELY (no blank page) and shows 同步中', () => {
    saveSnapshot({
      goal: '把发布会改到周五', phase: 't1', status: 'compiling',
      skeletons: [{ label: '发布日期' }], nodes: [], checkpoints: [],
    });
    render(<TaskExperience />);
    // the goal card shows the LAST goal instantly — not the waiting text
    expect(screen.getByTestId('goal-text').textContent)
      .toBe('把发布会改到周五');
    // the T1 skeleton is already on screen
    expect(screen.getAllByTestId('var-skeleton').length).toBe(1);
    // and the honest badge says the truth is still syncing
    expect(screen.getByTestId('sync-badge')).toBeInTheDocument();
  });

  it('the first live signal retires the 同步中 badge', async () => {
    saveSnapshot({
      goal: '旧目标', phase: 't0', status: 'compiling',
      skeletons: [], nodes: [], checkpoints: [],
    });
    render(<TaskExperience />);
    expect(screen.getByTestId('sync-badge')).toBeInTheDocument();
    const es = FakeEventSource.instances.at(-1)!;
    es.emit('progress', { stage: 'goal', goal: '新目标' });
    await waitFor(() => {
      expect(screen.queryByTestId('sync-badge')).not.toBeInTheDocument();
    });
    expect(screen.getByTestId('goal-text').textContent).toBe('新目标');
  });

  it('without a snapshot there is no badge (fresh boot is not "syncing")', () => {
    render(<TaskExperience />);
    expect(screen.queryByTestId('sync-badge')).not.toBeInTheDocument();
    expect(screen.getByTestId('goal-text').textContent)
      .toContain('等待第一条任务指令');
  });
});
