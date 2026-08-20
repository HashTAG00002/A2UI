/**
 * governanceEvents — the A7 motion state machine's contract locks:
 *  - the CLOSED celebration gate (only final_pass 🎉 / checkpoint small
 *    reward; everything else NEVER celebrates — enumerated, not sampled);
 *  - rollback truncates checkpoints + arms playback by user-visible label;
 *  - pause/resume/stop lifecycle flags;
 *  - parseGovernanceSignal drops malformed frames (never a guess);
 *  - the SSE listener wiring (frozen contract: event: governance).
 */
import { describe, expect, it, vi, afterEach } from 'vitest';
import {
  celebrationFor,
  GOVERNANCE_KINDS,
  INITIAL_GOVERNANCE_MOTION,
  parseGovernanceSignal,
  reduceGovernance,
  type GovernanceSignal,
} from '../a2ui/governanceEvents';
import { connectA2ui } from '../a2ui/transport';

function gov(kind: GovernanceSignal['kind'], label?: string): GovernanceSignal {
  return { type: 'governance', kind, label };
}

describe('celebrationFor — the CLOSED celebration gate', () => {
  it('final_pass → the full celebration', () => {
    expect(celebrationFor('final_pass')).toBe('final_pass');
  });

  it('checkpoint_reached → the small reward', () => {
    expect(celebrationFor('checkpoint_reached')).toBe('checkpoint_reward');
  });

  it('EVERY other kind never celebrates (enumerated, not sampled)', () => {
    const negative = GOVERNANCE_KINDS.filter(
      (k) => k !== 'final_pass' && k !== 'checkpoint_reached',
    );
    expect(negative).toEqual([
      'checkpoint_added',
      'rollback',
      'pause',
      'resume',
      'stop',
      'node_verified',
      'node_failed',
      'final_fail',
    ]);
    for (const kind of negative) {
      expect(celebrationFor(kind), `kind=${kind}`).toBe('none');
    }
  });
});

describe('reduceGovernance — the motion state machine', () => {
  it('checkpoint_added appends by label, idempotently', () => {
    let s = reduceGovernance(INITIAL_GOVERNANCE_MOTION, gov('checkpoint_added', 'cp1'));
    s = reduceGovernance(s, gov('checkpoint_added', 'cp1'));
    s = reduceGovernance(s, gov('checkpoint_added', 'cp2'));
    expect(s.checkpoints).toEqual([
      { label: 'cp1', reached: false },
      { label: 'cp2', reached: false },
    ]);
  });

  it('checkpoint_reached marks the chip + arms the SMALL reward', () => {
    let s = reduceGovernance(INITIAL_GOVERNANCE_MOTION, gov('checkpoint_added', 'cp1'));
    s = reduceGovernance(s, gov('checkpoint_reached', 'cp1'));
    expect(s.checkpoints).toEqual([{ label: 'cp1', reached: true }]);
    expect(s.celebration).toEqual({
      kind: 'checkpoint_reward',
      nonce: 1,
      label: 'cp1',
    });
  });

  it('rollback truncates after the target and arms playback by label', () => {
    let s = INITIAL_GOVERNANCE_MOTION;
    for (const label of ['cp1', 'cp2', 'cp3']) {
      s = reduceGovernance(s, gov('checkpoint_added', label));
    }
    s = reduceGovernance(s, gov('rollback', 'cp2'));
    expect(s.checkpoints.map((c) => c.label)).toEqual(['cp1', 'cp2']);
    expect(s.rollback).toEqual({ active: true, targetLabel: 'cp2', nonce: 1 });
    // a rollback NEVER celebrates — and never bumps the nonce
    expect(s.celebration.nonce).toBe(0);
    expect(s.celebration.kind).toBe('none');
  });

  it('rollback with an unknown label honestly disables playback', () => {
    const s = reduceGovernance(INITIAL_GOVERNANCE_MOTION, gov('checkpoint_added', 'cp1'));
    const after = reduceGovernance(s, gov('rollback', '不存在'));
    expect(after.rollback.active).toBe(false);
    expect(after.rollback.targetLabel).toBeNull();
  });

  it('pause / resume / stop flip the lifecycle flags', () => {
    let s = reduceGovernance(INITIAL_GOVERNANCE_MOTION, gov('pause'));
    expect(s.paused).toBe(true);
    s = reduceGovernance(s, gov('resume'));
    expect(s.paused).toBe(false);
    s = reduceGovernance(s, gov('stop'));
    expect(s.stopped).toBe(true);
    expect(s.paused).toBe(false);
  });

  it('node_failed surfaces the failure label and NEVER celebrates', () => {
    const s = reduceGovernance(INITIAL_GOVERNANCE_MOTION, gov('node_failed', '校验日期'));
    expect(s.failureLabel).toBe('校验日期');
    expect(s.celebration.kind).toBe('none');
  });

  it('final_pass arms the full celebration exactly once per event', () => {
    let s = reduceGovernance(INITIAL_GOVERNANCE_MOTION, gov('final_pass', '全部通过'));
    expect(s.celebration).toEqual({ kind: 'final_pass', nonce: 1, label: '全部通过' });
    expect(s.stopped).toBe(true);
    s = reduceGovernance(s, gov('final_pass', '重复事件'));
    expect(s.celebration.nonce).toBe(2);
  });

  it('final_fail cancels any pending celebration and marks failure (negative gate)', () => {
    let s = reduceGovernance(INITIAL_GOVERNANCE_MOTION, gov('checkpoint_reached', 'cp1'));
    s = reduceGovernance(s, gov('final_fail', '校验失败'));
    expect(s.celebration.kind).toBe('none');
    expect(s.failureLabel).toBe('校验失败');
    expect(s.stopped).toBe(true);
  });
});

describe('parseGovernanceSignal — malformed frames are dropped', () => {
  it('parses a well-formed frozen-contract frame', () => {
    const sig = parseGovernanceSignal({
      type: 'governance',
      kind: 'checkpoint_reached',
      label: 'cp1',
      rev: 3,
      ts: 1755000000000,
      detail: { any: 'thing' },
    });
    expect(sig).toEqual({
      type: 'governance',
      kind: 'checkpoint_reached',
      label: 'cp1',
      rev: 3,
      ts: 1755000000000,
      detail: { any: 'thing' },
    });
  });

  it('drops non-objects, wrong type, and unknown kinds', () => {
    expect(parseGovernanceSignal(null)).toBeNull();
    expect(parseGovernanceSignal('governance')).toBeNull();
    expect(parseGovernanceSignal({ type: 'a2ui', kind: 'pause' })).toBeNull();
    expect(parseGovernanceSignal({ type: 'governance', kind: 'mystery' })).toBeNull();
  });
});

describe('connectA2ui governance listener (frozen SSE contract wiring)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  class FakeEventSource {
    static instances: FakeEventSource[] = [];
    listeners: Record<string, Array<(ev: { data: string }) => void>> = {};
    onopen: (() => void) | null = null;
    onmessage: ((ev: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    url: string;
    constructor(url: string) {
      this.url = url;
      FakeEventSource.instances.push(this);
    }
    addEventListener(type: string, fn: (ev: { data: string }) => void) {
      (this.listeners[type] ??= []).push(fn);
    }
    close() {}
    emit(type: string, data: string) {
      for (const fn of this.listeners[type] ?? []) fn({ data });
    }
  }

  it('forwards parsed governance frames and drops malformed ones', () => {
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource);
    const onGovernance = vi.fn();
    const onProgress = vi.fn();
    connectA2ui({
      onMessages: () => {},
      onProgress,
      onGovernance,
    });
    const es = FakeEventSource.instances.at(-1)!;

    es.emit('governance', JSON.stringify({ type: 'governance', kind: 'pause' }));
    expect(onGovernance).toHaveBeenCalledTimes(1);
    expect(onGovernance).toHaveBeenCalledWith({ type: 'governance', kind: 'pause' });

    es.emit('governance', JSON.stringify({ type: 'governance', kind: 'nope' }));
    es.emit('governance', 'not json');
    expect(onGovernance).toHaveBeenCalledTimes(1); // dropped, never guessed

    // the progress listener is untouched by governance frames
    expect(onProgress).not.toHaveBeenCalled();
  });
});
