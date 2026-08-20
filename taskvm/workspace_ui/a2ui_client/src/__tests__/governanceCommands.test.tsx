/**
 * governanceCommands — the A9.1 optimistic first-response contract:
 *
 *  1. <100ms VISIBLE receipt: the click flips the button to its pending
 *     state IMMEDIATELY (before the POST resolves — that is the whole
 *     point: the receipt is local, the truth arrives later);
 *  2. honest rollback: a `{ok:false}` answer restores the pre-click
 *     status and surfaces the server's reason verbatim;
 *  3. success settles: the pending chip clears and the receipt echoes
 *     the driver state;
 *  4. zero model calls: the command path touches ONLY the local
 *     governance proxy route — never a model endpoint.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TaskExperience } from '../TaskExperience';
import { postGovernance } from '../a2ui/governanceApi';

// ── the SSE transport is faked: we drive progress signals by hand ────
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, Array<(ev: { data: string }) => void>>();
  closed = false;

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
  open() { this.onopen?.(); }
  close() { this.closed = true; }
}

/** A governance button by its FIXED action attribute (the shell's
 *  iron-law selector — independent of the pending label suffix). */
function govBtn(action: string): HTMLButtonElement {
  const el = document.querySelector(
    `[data-governance-action="${action}"]`,
  );
  if (!(el instanceof HTMLButtonElement)) {
    throw new Error(`governance button ${action} not rendered`);
  }
  return el;
}

const fetchCalls: string[] = [];

function mockFetch(handlers: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    fetchCalls.push(url);
    for (const [prefix, body] of Object.entries(handlers)) {
      if (url.includes(prefix)) {
        if (body instanceof Error) throw body;
        return {
          ok: true,
          status: 200,
          json: async () => body,
          blob: async () => new Blob(['x']),
        } as Response;
      }
    }
    return {
      ok: true, status: 200,
      json: async () => ({ ok: true }),
      blob: async () => new Blob(['x']),
    } as Response;
  }));
}

/** Drive the island to a Ready surface (goal → ready) through the fake
 *  SSE connection — the same signals the real server sends. */
async function driveToReady() {
  const es = FakeEventSource.instances.at(-1)!;
  es.emit('progress', { stage: 'goal', goal: '把发布会改到周五' });
  es.emit('progress', { stage: 't1', variables: [{ label: '发布日期' }] });
  es.emit('progress', { stage: 't2', nodes: [
    { label: '修改日期', kind: 'step', status: 'waiting' },
  ] });
  es.emit('progress', { stage: 'ready', surfaceId: 'taskvm-task-app' });
  await waitFor(() => {
    expect(govBtn('start')).toBeEnabled();
  });
}

beforeEach(() => {
  FakeEventSource.instances = [];
  fetchCalls.length = 0;
  localStorage.clear();
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('A9.1 optimistic governance commands', () => {
  it('<100ms receipt: the click flips Start to pending IMMEDIATELY while the POST is still in flight', async () => {
    // the proxy route NEVER resolves — the receipt must not depend on it
    const never = new Promise<Response>(() => {});
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      fetchCalls.push(String(input));
      return never as unknown as Response;
    }));
    render(<TaskExperience />);
    await driveToReady();

    // a native synchronous click: the React handler runs INLINE, so the
    // pending receipt is on the DOM before the next line — that is the
    // <100ms contract (userEvent's pointer-event machinery would add
    // its own overhead to the measurement, not to the receipt).
    const btn = govBtn('start');
    const t0 = performance.now();
    act(() => {
      btn.click();
    });
    const after = govBtn('start');
    expect(after.dataset.pending).toBe('true');
    expect(after.getAttribute('aria-busy')).toBe('true');
    expect(performance.now() - t0).toBeLessThan(100);
    expect(after.textContent).toContain('…');
  });

  it('honest rollback: ok:false restores the pre-click status and surfaces the reason', async () => {
    mockFetch({
      '/api/app/governance/start': { ok: false, action: 'start',
        error: '任务目标已变更，等待重新编排后才能继续执行' },
    });
    const user = userEvent.setup();
    render(<TaskExperience />);
    await driveToReady();

    await user.click(govBtn('start'));
    await waitFor(() => {
      expect(screen.getByTestId('last-action').textContent)
        .toContain('等待重新编排');
    });
    // rolled back: pending cleared AND the Start button is pressable
    // again (the status pill returned to Ready — no zombie optimism)
    expect(govBtn('start').dataset.pending).toBe('false');
    await waitFor(() => {
      expect(govBtn('start')).toBeEnabled();
    });
  });

  it('success settles: pending clears and the receipt echoes the driver state', async () => {
    mockFetch({
      '/api/app/governance/start': { ok: true, action: 'start',
        state: 'running' },
    });
    const user = userEvent.setup();
    render(<TaskExperience />);
    await driveToReady();

    await user.click(govBtn('start'));
    await waitFor(() => {
      expect(screen.getByTestId('last-action').textContent)
        .toContain('start ✓ (running)');
    });
    expect(govBtn('start').dataset.pending).toBe('false');
  });

  it('ZERO model calls: the command path touches only the local proxy route', async () => {
    mockFetch({
      '/api/app/governance/pause': { ok: true, action: 'pause',
        state: 'paused' },
    });
    const user = userEvent.setup();
    render(<TaskExperience />);
    await driveToReady();

    // pause needs running status — the optimistic start flip is enough
    mockFetch({
      '/api/app/governance/start': { ok: true, action: 'start',
        state: 'running' },
      '/api/app/governance/pause': { ok: true, action: 'pause',
        state: 'paused' },
    });
    await user.click(govBtn('start'));
    await waitFor(() => {
      expect(govBtn('pause')).toBeEnabled();
    });
    await user.click(govBtn('pause'));
    await waitFor(() => {
      expect(screen.getByTestId('last-action').textContent)
        .toContain('pause ✓');
    });
    const gov = fetchCalls.filter((u) => u.includes('/api/app/governance/'));
    expect(gov.length).toBeGreaterThanOrEqual(2);
    // no model/provider endpoint was ever contacted by the shell chrome
    expect(fetchCalls.some((u) => /chat|completion|model|v1\//i.test(u)))
      .toBe(false);
  });
});

describe('postGovernance — transport totalness', () => {
  it('a network failure resolves to {ok:false} — never a throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('network down');
    }));
    const res = await postGovernance('stop');
    expect(res.ok).toBe(false);
    expect(res.error).toContain('network down');
  });

  it('a malformed body resolves to {ok:false} with the HTTP status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      status: 500,
      json: async () => { throw new Error('not json'); },
    }) as unknown as Response));
    const res = await postGovernance('stop');
    expect(res.ok).toBe(false);
    expect(res.error).toBe('HTTP 500');
  });
});
