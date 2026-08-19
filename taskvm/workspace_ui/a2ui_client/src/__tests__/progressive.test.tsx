/**
 * ProgressiveTaskPlane — the honest skeleton laws (workplan §20.1):
 *  - T0 renders in <100ms (pure local, no data dependency);
 *  - T0/T1 NEVER fabricate plan content (no values, no fake nodes);
 *  - T2 shows exactly the nodes the "architect" returned, with kinds;
 *  - live hands the region to the A2UI surface.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  ProgressiveTaskPlane,
  type WorkflowNodeChip,
} from '../progressive/ProgressiveTaskPlane'

const VARS = [
  { label: '发布日期' },
  { label: '通知名单' },
  { label: '预算' },
]

const NODES: WorkflowNodeChip[] = [
  { label: '修改发布日期', kind: 'step', status: 'waiting' },
  { label: '日期确认点', kind: 'checkpoint', status: 'waiting' },
  { label: '校验通知名单', kind: 'verification', status: 'waiting' },
]

describe('T0 — instant, honest compile feedback', () => {
  it('renders the pulsing compile node in under 100ms (pure local)', () => {
    const t0 = performance.now()
    render(<ProgressiveTaskPlane phase="t0" variableSkeletons={VARS} workflowNodes={NODES} />)
    const elapsed = performance.now() - t0

    expect(screen.getByTestId('t0-pulse')).toBeTruthy()
    expect(screen.getByText('正在编译任务世界…')).toBeTruthy()
    expect(elapsed).toBeLessThan(100)
  })

  it('shows NO variable labels and NO workflow nodes at T0 (no fabrication)', () => {
    render(<ProgressiveTaskPlane phase="t0" variableSkeletons={VARS} workflowNodes={NODES} />)
    expect(screen.queryAllByTestId('var-skeleton')).toHaveLength(0)
    expect(screen.queryByText('修改发布日期')).toBeNull()
    expect(screen.queryByText('发布日期')).toBeNull()
  })
})

describe('T1 — variable skeletons land with pending placeholders', () => {
  it('shows labels but never values', () => {
    render(<ProgressiveTaskPlane phase="t1" variableSkeletons={VARS} workflowNodes={NODES} />)
    const skeletons = screen.getAllByTestId('var-skeleton')
    expect(skeletons).toHaveLength(3)
    expect(screen.getByText('发布日期')).toBeTruthy()
    expect(screen.getByText('通知名单')).toBeTruthy()
    // pending placeholder present; no fake dates/lists/numbers anywhere
    expect(screen.getAllByText('···').length).toBe(3)
    expect(screen.queryByText(/2026/)).toBeNull()
    expect(screen.queryByText(/人|元/)).toBeNull()
  })

  it('still hides the DAG at T1 (architect has not returned)', () => {
    render(<ProgressiveTaskPlane phase="t1" variableSkeletons={VARS} workflowNodes={NODES} />)
    expect(screen.queryByText('修改发布日期')).toBeNull()
    expect(screen.queryByTestId('t2-dag')).toBeNull()
  })
})

describe('T2 — the dot morphs into the real DAG', () => {
  it('renders exactly the returned nodes with their kinds', () => {
    render(<ProgressiveTaskPlane phase="t2" variableSkeletons={VARS} workflowNodes={NODES} />)
    expect(screen.getByText('修改发布日期')).toBeTruthy()
    expect(screen.getByText('日期确认点')).toBeTruthy()
    expect(screen.getByText('校验通知名单')).toBeTruthy()
    expect(
      document.querySelectorAll('[data-kind="step"], [data-kind="checkpoint"], [data-kind="verification"]').length,
    ).toBe(3)
    // compile pulse is gone
    expect(screen.queryByTestId('t0-pulse')).toBeNull()
  })

  it('renders NO nodes when the architect returned none (honest empty)', () => {
    render(<ProgressiveTaskPlane phase="t2" variableSkeletons={VARS} workflowNodes={[]} />)
    expect(screen.queryByTestId('t2-dag')).toBeNull()
    expect(screen.getAllByTestId('var-skeleton')).toHaveLength(3)
  })
})

describe('live — the region is handed to the A2UI surface', () => {
  it('renders the live payload instead of any skeleton', () => {
    render(
      <ProgressiveTaskPlane
        phase="live"
        variableSkeletons={VARS}
        workflowNodes={NODES}
        live={<div data-testid="a2ui-surface-stub">surface</div>}
      />,
    )
    expect(screen.getByTestId('plane-live')).toBeTruthy()
    expect(screen.getByTestId('a2ui-surface-stub')).toBeTruthy()
    expect(screen.queryByTestId('t1-vars')).toBeNull()
    expect(screen.queryByTestId('t2-dag')).toBeNull()
  })
})
