/**
 * IntentConsole — the A6 frontend half's contract locks:
 *  - the FIVE kinds render distinct structured paths (local_patch /
 *    goal_patch / checkpoint / rollback / clarify);
 *  - pending state (请求中) and failure surfaces (4xx body / network
 *    error) render honestly;
 *  - the real transport POSTs the frozen body {"text": ...} to the
 *    frozen URL;
 *  - the console is fixed shell chrome: inside the governance shell,
 *    OUTSIDE the dynamic task region — the shell's iron laws hold.
 */
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { IntentConsole } from '../intent/IntentConsole';
import {
  MOCK_INTENT_RESPONSES,
  createMockIntentTransport,
} from '../intent/mockIntentTransport';
import { realIntentTransport } from '../intent/intentTransport';
import type { IntentResponse } from '../intent/intentTransport';
import { GovernanceShell } from '../governance/GovernanceShell';

afterEach(() => {
  vi.unstubAllGlobals();
});

async function submit(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByTestId('intent-input'), text);
  await user.click(screen.getByTestId('intent-submit'));
}

describe('the FIVE kinds render distinct structured paths', () => {
  it.each([
    ['local_patch', 'intent-result'],
    ['goal_patch', 'intent-result'],
    ['checkpoint', 'intent-result'],
    ['rollback', 'intent-result'],
    ['clarify', 'intent-clarify'],
  ] as const)('%s renders through %s', async (kind, testid) => {
    const transport = createMockIntentTransport([
      MOCK_INTENT_RESPONSES[kind],
    ]);
    render(<IntentConsole transport={transport} />);
    await submit('把发布日期改到 8 月 30 日');
    await waitFor(() => {
      expect(screen.getByTestId(testid)).toBeTruthy();
    });
    expect(screen.getByTestId(testid).getAttribute('data-kind') ?? 'clarify')
      .toBe(kind);
    expect(transport.calls).toEqual(['把发布日期改到 8 月 30 日']);
  });

  it('local_patch echoes the updates + rationale', async () => {
    render(
      <IntentConsole transport={createMockIntentTransport([MOCK_INTENT_RESPONSES.local_patch])} />,
    );
    await submit('改日期');
    await waitFor(() => expect(screen.getByTestId('intent-result')).toBeTruthy());
    expect(screen.getByText('release_date')).toBeTruthy();
    expect(screen.getByText('2026-08-30')).toBeTruthy();
    expect(screen.getByTestId('intent-result').textContent).toContain('推迟');
  });

  it('goal_patch echoes the goal + constraint chips', async () => {
    render(
      <IntentConsole transport={createMockIntentTransport([MOCK_INTENT_RESPONSES.goal_patch])} />,
    );
    await submit('加个预算约束');
    await waitFor(() => expect(screen.getByTestId('intent-result')).toBeTruthy());
    expect(
      screen.getByText('把发布会日期改到 8 月底并通知所有参会人，控制在预算内'),
    ).toBeTruthy();
    expect(screen.getByText('预算不超过 5000 元')).toBeTruthy();
  });

  it('checkpoint / rollback echo the checkpoint label', async () => {
    render(
      <IntentConsole transport={createMockIntentTransport([MOCK_INTENT_RESPONSES.rollback])} />,
    );
    await submit('回到刚才');
    await waitFor(() => expect(screen.getByTestId('intent-result')).toBeTruthy());
    expect(screen.getByText('日期已确认')).toBeTruthy();
  });

  it('clarify renders the question card + the no-execution note', async () => {
    render(
      <IntentConsole transport={createMockIntentTransport([MOCK_INTENT_RESPONSES.clarify])} />,
    );
    await submit('改一下日期');
    await waitFor(() => expect(screen.getByTestId('intent-clarify')).toBeTruthy());
    expect(screen.getByTestId('intent-clarify').textContent).toContain('8 月 28 日');
    expect(screen.getByTestId('intent-clarify').textContent).toContain('不会执行任何变更');
    // a clarify executes NOTHING — no result card alongside
    expect(screen.queryByTestId('intent-result')).toBeNull();
  });
});

describe('pending + failure states', () => {
  it('shows 解析中 while the request is in flight and disables submit', async () => {
    let resolve!: (r: IntentResponse) => void;
    const transport = {
      postIntent: () =>
        new Promise<IntentResponse>((res) => {
          resolve = res;
        }),
    };
    render(<IntentConsole transport={transport} />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId('intent-input'), '在途请求');
    await user.click(screen.getByTestId('intent-submit'));
    expect(screen.getByTestId('intent-pending').textContent).toContain('解析中');
    expect(screen.getByTestId('intent-submit')).toBeDisabled();

    await act(async () => {
      resolve(MOCK_INTENT_RESPONSES.checkpoint);
    });
    await waitFor(() => expect(screen.queryByTestId('intent-pending')).toBeNull());
    expect(screen.getByTestId('intent-result')).toBeTruthy();
  });

  it('renders a 4xx {ok:false,error} body as the error surface', async () => {
    render(
      <IntentConsole transport={createMockIntentTransport([MOCK_INTENT_RESPONSES.error])} />,
    );
    await submit('改预算');
    await waitFor(() => expect(screen.getByTestId('intent-error')).toBeTruthy());
    expect(screen.getByTestId('intent-error').textContent).toContain('readonly');
  });

  it('renders a network rejection honestly (no fake success)', async () => {
    const transport = {
      postIntent: () => Promise.reject(new Error('网络中断')),
    };
    render(<IntentConsole transport={transport} />);
    await submit('任何话');
    await waitFor(() => expect(screen.getByTestId('intent-error')).toBeTruthy());
    expect(screen.getByTestId('intent-error').textContent).toContain('网络中断');
  });
});

describe('the real transport POSTs the frozen contract', () => {
  it('POSTs {"text": ...} to /api/app/a2ui/intent with JSON headers', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => MOCK_INTENT_RESPONSES.clarify,
    });
    vi.stubGlobal('fetch', fetchMock);
    const answer = await realIntentTransport.postIntent('测试文本');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [
      string,
      RequestInit,
    ];
    expect(url).toBe('/api/app/a2ui/intent');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body as string)).toEqual({ text: '测试文本' });
    expect(answer).toEqual(MOCK_INTENT_RESPONSES.clarify);
  });

  it('surfaces a non-JSON error body as HTTP <status>', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 501,
      json: async () => {
        throw new Error('not json');
      },
    });
    vi.stubGlobal('fetch', fetchMock);
    const answer = await realIntentTransport.postIntent('x');
    expect(answer).toEqual({ ok: false, error: 'HTTP 501' });
  });
});

describe('the console is FIXED shell chrome (shell laws hold)', () => {
  function noop() {}

  it('renders inside the governance shell, OUTSIDE the dynamic region', () => {
    render(
      <GovernanceShell
        goal="g"
        status="ready"
        canStart
        onStart={noop}
        onPause={noop}
        onResume={noop}
        onStop={noop}
        onCheckpoint={noop}
        onRollback={noop}
        checkpoints={[]}
        evidenceCount={0}
        onOpenEvidence={noop}
        substrateLabel="MobileGym"
        onOpenSubstrate={noop}
        intentConsole={<IntentConsole transport={createMockIntentTransport()} />}
      >
        <div>dynamic-region</div>
      </GovernanceShell>,
    );
    const shell = document.querySelector('[data-testid="governance-shell"]')!;
    const consoleSlot = document.querySelector('[data-testid="intent-console-slot"]')!;
    const dynamic = document.querySelector('[data-testid="dynamic-task-region"]')!;
    expect(shell.contains(consoleSlot)).toBe(true);
    expect(dynamic.contains(consoleSlot)).toBe(false);
    // every governance control is still present
    for (const action of ['start', 'pause', 'resume', 'stop', 'checkpoint', 'rollback']) {
      expect(
        document.querySelector(`[data-governance-action="${action}"]`),
      ).not.toBeNull();
    }
  });

  it('without the slot prop the shell renders exactly as before', () => {
    render(
      <GovernanceShell
        goal="g"
        status="ready"
        canStart
        onStart={noop}
        onPause={noop}
        onResume={noop}
        onStop={noop}
        onCheckpoint={noop}
        onRollback={noop}
        checkpoints={[]}
        evidenceCount={0}
        onOpenEvidence={noop}
        substrateLabel="MobileGym"
        onOpenSubstrate={noop}
      >
        <div />
      </GovernanceShell>,
    );
    expect(document.querySelector('[data-testid="intent-console-slot"]')).toBeNull();
  });
});
