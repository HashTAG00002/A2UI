/**
 * snake — the PURE verified-progress contract locks (A7 acceptance:
 * "verified snake progress 只跨 verified milestone；进行中节点显示探出
 * 头但不越界").
 */
import { describe, expect, it } from 'vitest';
import {
  applyGovernanceToNodes,
  POKE_MAX,
  rollbackTargetIndex,
  snakeHeadUnits,
  snakeModel,
} from '../a2ui/snake';
import type { WorkflowNodeChip } from '../progressive/ProgressiveTaskPlane';

function nodes(...statuses: WorkflowNodeChip['status'][]): WorkflowNodeChip[] {
  return statuses.map((status, i) => ({
    label: `节点${i + 1}`,
    kind: i === 2 ? 'checkpoint' : 'step',
    status,
  }));
}

describe('snakeModel — the body only ever lies over verified nodes', () => {
  it('crosses exactly the contiguous verified prefix', () => {
    const m = snakeModel(nodes('verified', 'verified', 'waiting', 'executing'));
    expect(m.crossed).toBe(2);
  });

  it('NEVER crosses a verified node that sits behind an unverified one (a snake cannot skip)', () => {
    // node3 verified but node1 waiting — the trajectory stays at 1
    const m = snakeModel(nodes('verified', 'waiting', 'verified', 'verified'));
    expect(m.crossed).toBe(1);
  });

  it('an EXECUTING node ahead shows the head poking, never crossing', () => {
    const m = snakeModel(nodes('verified', 'executing', 'waiting'));
    expect(m.pokeIndex).toBe(1);
    const head = snakeHeadUnits(m);
    // strictly below the next milestone (2) — 探出头但不越界
    expect(head).toBe(1 + POKE_MAX);
    expect(head).toBeLessThan(2);
    expect(POKE_MAX).toBeLessThan(1);
  });

  it('a WAITING node ahead gets NO poke (no fabricated activity)', () => {
    const m = snakeModel(nodes('verified', 'waiting'));
    expect(m.pokeIndex).toBeNull();
    expect(snakeHeadUnits(m)).toBe(1);
  });

  it('a FAILED node ahead gets NO poke and is reported as the failure', () => {
    const m = snakeModel(nodes('verified', 'failed', 'waiting'));
    expect(m.pokeIndex).toBeNull();
    expect(m.failedIndex).toBe(1);
    expect(m.crossed).toBe(1);
  });

  it('all-verified means every milestone is crossed and nothing pokes', () => {
    const m = snakeModel(nodes('verified', 'verified', 'verified'));
    expect(m.crossed).toBe(3);
    expect(m.pokeIndex).toBeNull();
    expect(snakeHeadUnits(m)).toBe(2); // the head sits ON the last milestone
  });

  it('the very first node executing still shows a bounded poke from zero', () => {
    const m = snakeModel(nodes('executing', 'waiting'));
    expect(m.crossed).toBe(0);
    expect(m.pokeIndex).toBe(0);
    expect(snakeHeadUnits(m)).toBe(POKE_MAX);
  });

  it('an empty node list is an empty snake (honest nothing)', () => {
    const m = snakeModel([]);
    expect(m.crossed).toBe(0);
    expect(snakeHeadUnits(m)).toBe(0);
  });
});

describe('applyGovernanceToNodes — node truth merges BY LABEL', () => {
  const base = nodes('waiting', 'waiting');

  it('node_verified flips the matching label to verified', () => {
    const out = applyGovernanceToNodes(base, {
      type: 'governance',
      kind: 'node_verified',
      label: '节点2',
    });
    expect(out[1].status).toBe('verified');
    expect(out[0].status).toBe('waiting');
  });

  it('node_failed flips the matching label to failed', () => {
    const out = applyGovernanceToNodes(base, {
      type: 'governance',
      kind: 'node_failed',
      label: '节点1',
    });
    expect(out[0].status).toBe('failed');
  });

  it('unknown labels change NOTHING (never a guess)', () => {
    const out = applyGovernanceToNodes(base, {
      type: 'governance',
      kind: 'node_verified',
      label: '不存在的节点',
    });
    expect(out).toBe(base);
  });

  it('non-node governance kinds are a no-op', () => {
    const out = applyGovernanceToNodes(base, {
      type: 'governance',
      kind: 'pause',
    });
    expect(out).toBe(base);
  });
});

describe('rollbackTargetIndex — playback targets resolve by label', () => {
  const base: WorkflowNodeChip[] = [
    { label: '修改发布日期', kind: 'step', status: 'verified' },
    { label: '校验日期合法', kind: 'verification', status: 'verified' },
    { label: '日期确认点', kind: 'checkpoint', status: 'verified' },
  ];

  it('resolves the node index for a known checkpoint label', () => {
    expect(rollbackTargetIndex(base, '日期确认点')).toBe(2);
  });

  it('returns null for an unknown label — playback honestly disables', () => {
    expect(rollbackTargetIndex(base, '不存在的检查点')).toBeNull();
    expect(rollbackTargetIndex(base, null)).toBeNull();
  });
});
