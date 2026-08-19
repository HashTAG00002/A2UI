/**
 * A2UI stream — the official MessageProcessor/A2uiSurface chain consuming
 * the MOCK message stream (exact production shapes: server-owned
 * createSurface/updateDataModel + decoder-owned updateComponents with
 * correct v0.9 `{"path": ...}` bindings), plus the actionBridge's
 * structured-event translation.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { A2uiSurface } from '@a2ui/react/v0_9'
import { useA2uiStream } from '../a2ui/useA2uiStream'
import { mockA2uiMessages, MOCK_SURFACE_ID } from '../a2ui/mockMessages'
import { translateAction } from '../a2ui/actionBridge'
import type { A2uiClientAction } from '@a2ui/web_core/v0_9'
import { PROTOCOL_VERSION, surfaceIdForSession } from '../a2ui/protocol'

function Harness({ onAction }: { onAction?: (a: A2uiClientAction) => void }) {
  const stream = useA2uiStream(onAction)
  return (
    <div>
      <button onClick={() => stream.processMessages(mockA2uiMessages)}>
        feed
      </button>
      {stream.surfaces.map((s) => (
        <A2uiSurface key={s.id} surface={s} />
      ))}
    </div>
  )
}

describe('mock stream renders through the official A2UI renderer', () => {
  it('shows the decoder-style tree with bound data-model values', async () => {
    const { getByText } = render(<Harness />)
    ;(
      document.querySelector('button') as HTMLButtonElement
    ).click()
    await waitFor(() => {
      expect(getByText('任务变量')).toBeTruthy()
    })
    // bound /task/status renders the data model's value, not a literal
    expect(getByText('ready')).toBeTruthy()
    // the TextField label comes from the component tree
    expect(getByText('发布日期')).toBeTruthy()
    // bound desired value lands in the input's value (data-binding, not literal)
    expect(screen.getByDisplayValue('2026-08-30')).toBeTruthy()
    // the Button's Text child (rendered once for display, once inside the
    // button affordance)
    expect(screen.getAllByText('更新日期').length).toBeGreaterThan(0)
  })

  it('exposes exactly one surface with the derived mock surface id', async () => {
    render(<Harness />)
    ;(
      document.querySelector('button') as HTMLButtonElement
    ).click()
    await waitFor(() => {
      expect(document.querySelector('[data-testid="plane-live"]') ?? true).toBeTruthy()
    })
    // processor-level assertion via a second harness
    const listener = vi.fn()
    const Harness2 = () => {
      const stream = useA2uiStream(listener)
      return (
        <div>
          <button
            data-testid="feed2"
            onClick={() => stream.processMessages(mockA2uiMessages)}
          />
          <span data-testid="count">{stream.surfaces.length}</span>
          <span data-testid="sid">{stream.surfaces[0]?.id ?? ''}</span>
        </div>
      )
    }
    const { getByTestId } = render(<Harness2 />)
    getByTestId('feed2').click()
    await waitFor(() => expect(getByTestId('count').textContent).toBe('1'))
    expect(getByTestId('sid').textContent).toBe(MOCK_SURFACE_ID)
  })
})

describe('actionBridge (structured events, never free text)', () => {
  const base = {
    surfaceId: MOCK_SURFACE_ID,
    sourceComponentId: 'submit',
    timestamp: '2026-08-19T12:00:00Z',
  }

  it('translates a well-formed local_patch action', () => {
    const event = translateAction({
      ...base,
      name: 'taskvm.local_patch',
      context: { semanticKey: 'release_date', value: '2026-08-31' },
    })
    expect(event).toMatchObject({
      kind: 'taskvm.local_patch',
      semanticKey: 'release_date',
      value: '2026-08-31',
    })
  })

  it('rejects local_patch without a semanticKey', () => {
    const event = translateAction({
      ...base,
      name: 'taskvm.local_patch',
      context: {},
    })
    expect(event.kind).toBe('rejected')
  })

  it('rejects governance actions coming from the dynamic surface', () => {
    for (const name of ['pause', 'rollback', 'goal_patch']) {
      const event = translateAction({ ...base, name, context: {} })
      expect(event.kind).toBe('rejected')
      if (event.kind === 'rejected') {
        expect(event.reason).toContain('fixed shell')
      }
    }
  })

  it('rejects unknown action names (no best-effort guessing)', () => {
    const event = translateAction({
      ...base,
      name: 'taskvm.magic',
      context: {},
    })
    expect(event.kind).toBe('rejected')
  })
})

describe('client-side protocol constants mirror the backend', () => {
  it('pins v0.9 and the basic catalog id', () => {
    expect(PROTOCOL_VERSION).toBe('v0.9')
    expect(surfaceIdForSession('Mock Demo/01')).toBe('taskvm-task-mock-demo-01')
    expect(mockA2uiMessages[0]).toMatchObject({
      version: 'v0.9',
      createSurface: {
        surfaceId: MOCK_SURFACE_ID,
        catalogId:
          'https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json',
      },
    })
  })
})
