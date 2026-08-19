/**
 * GovernanceShell — the fixed chrome's iron laws:
 *  1. EVERY governance control is ALWAYS in the DOM, whatever the state
 *     (only `disabled` flips — the model can never create/hide/remove
 *     the shell's affordances);
 *  2. Start stays disabled until the task world is compiled and Ready
 *     (no autostart, workplan §2/§P5);
 *  3. Rollback requires an existing checkpoint; Checkpoint requires
 *     running autonomy.
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import {
  GovernanceShell,
  type TaskStatus,
} from '../governance/GovernanceShell'

const ALL_ACTIONS = [
  'start',
  'pause',
  'resume',
  'stop',
  'checkpoint',
  'rollback',
  'open-evidence',
  'open-substrate',
] as const

function noop() {}

function renderShell(overrides: Partial<Parameters<typeof GovernanceShell>[0]> = {}) {
  const props = {
    goal: '把发布会日期改到 8 月底并通知所有参会人',
    status: 'ready' as TaskStatus,
    canStart: true,
    onStart: noop,
    onPause: noop,
    onResume: noop,
    onStop: noop,
    onCheckpoint: noop,
    onRollback: noop,
    checkpoints: [] as { label: string }[],
    evidenceCount: 2,
    onOpenEvidence: noop,
    substrateLabel: 'MobileGym',
    onOpenSubstrate: noop,
    children: <div>dynamic-region</div>,
    ...overrides,
  }
  return render(<GovernanceShell {...props} />)
}

describe('GovernanceShell permanence (never model-controlled)', () => {
  const everyStatus: TaskStatus[] = [
    'compiling',
    'ready',
    'running',
    'paused',
    'completed',
    'failed',
  ]

  it.each(everyStatus)('renders every control in status=%s', (status) => {
    renderShell({ status })
    for (const action of ALL_ACTIONS) {
      expect(
        document.querySelector(`[data-governance-action="${action}"]`),
        `missing control ${action}`,
      ).not.toBeNull()
    }
  })

  it('renders the goal text and the dynamic region as its child', () => {
    renderShell()
    expect(screen.getByTestId('goal-text').textContent).toContain('发布会日期')
    expect(screen.getByTestId('dynamic-task-region').textContent).toContain(
      'dynamic-region',
    )
  })

  it('shows the substrate affordance with its public label', () => {
    renderShell()
    expect(
      document.querySelector('[data-governance-action="open-substrate"]')
        ?.textContent,
    ).toContain('MobileGym')
  })
})

describe('start gating (Ready ≠ autostart)', () => {
  it('disables Start while the task world is still compiling', () => {
    renderShell({ status: 'compiling', canStart: false })
    const start = document.querySelector(
      '[data-governance-action="start"]',
    ) as HTMLButtonElement
    expect(start).toBeDisabled()
  })

  it('enables Start only when compiled and ready', () => {
    renderShell({ status: 'ready', canStart: true })
    const start = document.querySelector(
      '[data-governance-action="start"]',
    ) as HTMLButtonElement
    expect(start).toBeEnabled()
  })

  it('disables Start once running / paused / finished', () => {
    for (const status of ['running', 'paused', 'completed', 'failed'] as TaskStatus[]) {
      const { unmount } = renderShell({ status, canStart: true })
      const start = document.querySelector(
        '[data-governance-action="start"]',
      ) as HTMLButtonElement
      expect(start).toBeDisabled()
      unmount()
    }
  })
})

describe('control state machine', () => {
  it('Pause is only enabled while running', () => {
    renderShell({ status: 'ready' })
    const pause = document.querySelector(
      '[data-governance-action="pause"]',
    ) as HTMLButtonElement
    expect(pause).toBeDisabled()
  })

  it('Rollback requires a checkpoint to exist', () => {
    const { rerender } = renderShell({ checkpoints: [] })
    const rollback = () =>
      document.querySelector('[data-governance-action="rollback"]') as HTMLButtonElement
    expect(rollback()).toBeDisabled()

    rerender(
      <GovernanceShell
        goal="g"
        status="paused"
        canStart={false}
        onStart={noop}
        onPause={noop}
        onResume={noop}
        onStop={noop}
        onCheckpoint={noop}
        onRollback={noop}
        checkpoints={[{ label: '检查点 1' }]}
        evidenceCount={0}
        onOpenEvidence={noop}
        substrateLabel="MobileGym"
        onOpenSubstrate={noop}
      >
        <div />
      </GovernanceShell>,
    )
    expect(rollback()).toBeEnabled()
  })

  it('emits structured intents on click (never free text)', async () => {
    const user = userEvent.setup()
    const onStart = vi.fn()
    const onRollback = vi.fn()
    renderShell({ onStart, onRollback, checkpoints: [{ label: 'cp' }] })
    await user.click(
      document.querySelector('[data-governance-action="start"]')!,
    )
    await user.click(
      document.querySelector('[data-governance-action="rollback"]')!,
    )
    expect(onStart).toHaveBeenCalledTimes(1)
    expect(onRollback).toHaveBeenCalledTimes(1)
  })
})
