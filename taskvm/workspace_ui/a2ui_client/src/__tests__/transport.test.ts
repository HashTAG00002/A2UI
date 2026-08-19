/**
 * transport/morph — the §20.1 progressive-plane state machine under
 * test: every transition consumes a REAL server signal, skeletons never
 * fabricate values, and the plane never morphs backwards once live.
 */
import { describe, expect, it } from 'vitest';
import {
  INITIAL_ISLAND_STATE,
  reduceProgress,
  withSurfacesLive,
} from '../a2ui/morph';
import type { ProgressSignal } from '../a2ui/transport';

function reduceAll(events: ProgressSignal[]) {
  return events.reduce(reduceProgress, INITIAL_ISLAND_STATE);
}

describe('reduceProgress — real signals drive the morph chain', () => {
  it('T0: a goal signal resets the plane with the user\'s actual text', () => {
    const live = reduceAll([
      { stage: 'goal', goal: '旧目标' },
      { stage: 't1', variables: [{ label: '发布日期' }] },
      { stage: 'ready' },
    ]);
    const after = reduceProgress(live, { stage: 'goal', goal: '把会议改到周五' });
    expect(after).toEqual({
      ...INITIAL_ISLAND_STATE,
      goal: '把会议改到周五',
    });
  });

  it('T1: variable skeletons carry labels ONLY (values never fabricated)', () => {
    const s = reduceProgress(INITIAL_ISLAND_STATE, {
      stage: 't1',
      variables: [{ label: '发布日期' }, { label: '通知名单' }],
    });
    expect(s.phase).toBe('t1');
    expect(s.skeletons).toEqual([
      { label: '发布日期' },
      { label: '通知名单' },
    ]);
    // the payload itself carries no value fields — and the reducer
    // would not copy one even if it did
    expect(JSON.stringify(s.skeletons)).not.toContain('value');
  });

  it('T2: kernel DAG chips land with kind/status collapsed to the four known tones', () => {
    const s = reduceProgress(INITIAL_ISLAND_STATE, {
      stage: 't2',
      nodes: [
        { label: '修改日期', kind: 'step', status: 'waiting' },
        { label: '确认点', kind: 'checkpoint', status: 'ready' },
        { label: '校验', kind: 'weird-kind', status: 'also-weird' },
      ],
    });
    expect(s.phase).toBe('t2');
    expect(s.nodes).toEqual([
      { label: '修改日期', kind: 'step', status: 'waiting' },
      { label: '确认点', kind: 'checkpoint', status: 'waiting' },
      { label: '校验', kind: 'step', status: 'waiting' },
    ]);
  });

  it('ready: the plane goes live and compiling → ready', () => {
    const s = reduceAll([
      { stage: 'goal', goal: 'g' },
      { stage: 't1', variables: [{ label: 'x' }] },
      { stage: 't2', nodes: [] },
      { stage: 'ready', surfaceId: 'taskvm-task-app' },
    ]);
    expect(s.phase).toBe('live');
    expect(s.status).toBe('ready');
  });

  it('once live, replayed t1/t2 hints never morph the plane backwards', () => {
    const live = reduceAll([
      { stage: 'goal', goal: 'g' },
      { stage: 'ready' },
    ]);
    const poked = reduceProgress(live, {
      stage: 't1',
      variables: [{ label: '迟到的事件' }],
    });
    expect(poked).toBe(live); // structurally untouched — same object
  });

  it('goal_failed: honest failure surfaces the error and flips the pill', () => {
    const s = reduceProgress(INITIAL_ISLAND_STATE, {
      stage: 'goal_failed',
      error: 'HttpCUAModelError: boom',
    });
    expect(s.status).toBe('failed');
    expect(s.streamError).toBe('HttpCUAModelError: boom');
  });

  it('a2ui_failed: the surface error is inline but the goal stays healthy', () => {
    const s = reduceProgress(INITIAL_ISLAND_STATE, {
      stage: 'a2ui_failed',
      errors: ['policy: x', 'protocol: y'],
    });
    expect(s.status).toBe('compiling'); // not failed — kernel/runtime fine
    expect(s.streamError).toBe('policy: x；protocol: y');
  });

  it('unknown stages are ignored, never guessed', () => {
    const s = reduceProgress(INITIAL_ISLAND_STATE, {
      stage: 'something_new',
    });
    expect(s).toBe(INITIAL_ISLAND_STATE);
  });
});

describe('withSurfacesLive — A2UI messages are the authoritative evidence', () => {
  it('T2 + surfaces present → live/ready even if the ready hint was lost', () => {
    const s = withSurfacesLive({
      ...INITIAL_ISLAND_STATE,
      phase: 't2',
      goal: 'g',
    });
    expect(s.phase).toBe('live');
    expect(s.status).toBe('ready');
  });

  it('already live → untouched', () => {
    const live = { ...INITIAL_ISLAND_STATE, phase: 'live' as const };
    expect(withSurfacesLive(live)).toBe(live);
  });
});
