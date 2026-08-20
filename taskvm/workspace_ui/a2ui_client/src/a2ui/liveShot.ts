/**
 * liveShot — the island's thumbnail pipeline (A9.1):
 *
 *  - ≤240px server-side thumbnail (the APP shell's ``?thumb=1&w=240``
 *    route, JPEG ~15-40 KB instead of the ~2 MB full PNG);
 *  - content-hash dedup: the feed carries the current ``hash``; an
 *    unchanged screen means ZERO image bytes cross the wire (the old
 *    object URL is kept — "图片零丢失" while idle);
 *  - 150ms burst coalescing: rapid successive frames commit ONE image
 *    swap (the last wins), never a flicker of stale frames;
 *  - slow-network adaptation: the poll interval scales with the
 *    measured fetch RTT (EMA) — a degraded link backs off honestly
 *    instead of queueing requests.
 *
 * Pure helpers are exported for the contract tests; the hook wires
 * them to the feed endpoint (``GET /api/app/surface_shots``).
 */
import { useEffect, useRef, useState } from 'react';

export interface WallEntry {
  name: string;
  role: 'foreground' | 'background';
  hash: string;
  seq: number;
  thumbUrl: string;
  fullUrl: string;
}

export interface LiveShotState {
  /** The wall entry the feed last advertised (hash included). */
  entry: WallEntry | null;
  /** Object URL of the CURRENT decoded thumbnail (stable while the
   *  screen is unchanged — that is the zero-loss guarantee). */
  url: string | null;
  /** True while the very first thumbnail has not landed yet. */
  loading: boolean;
}

export const BASE_POLL_MS = 2500;
export const MAX_POLL_MS = 10000;
export const COALESCE_MS = 150;

/** Adaptive poll interval: a fast link stays at base cadence; a slow
 * link (EMA RTT above 1.5×base or above 2s) backs off, capped. */
export function computeNextPollMs(emaRttMs: number): number {
  if (emaRttMs <= 0 || emaRttMs < 1000) return BASE_POLL_MS;
  const scaled = Math.round(BASE_POLL_MS * (emaRttMs / 700));
  return Math.min(MAX_POLL_MS, Math.max(BASE_POLL_MS, scaled));
}

/** EMA update (α = 0.3): smooth, honest, single-sample friendly. */
export function nextEma(prev: number, sample: number): number {
  if (prev <= 0) return sample;
  return Math.round(prev * 0.7 + sample * 0.3);
}

/** True when the feed hash is unchanged — skip the image fetch
 * entirely (zero bytes, keep the previous object URL). */
export function isUnchanged(prev: WallEntry | null, next: WallEntry | null): boolean {
  if (prev === null || next === null) return false;
  return prev.hash === next.hash;
}

/** Create a coalescer: schedule `commit` at most once per
 * `COALESCE_MS`; the LAST scheduled payload wins (burst merging). */
export function createCoalescer<T>() {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let pending: T | null = null;
  return {
    schedule(value: T, commit: (v: T) => void): void {
      pending = value;
      if (timer !== null) return;
      timer = setTimeout(() => {
        timer = null;
        const v = pending;
        pending = null;
        if (v !== null) commit(v);
      }, COALESCE_MS);
    },
    cancel(): void {
      if (timer !== null) clearTimeout(timer);
      timer = null;
      pending = null;
    },
  };
}

/**
 * useLiveShot — poll the surface-shot feed, fetch thumbnails ONLY on
 * content change, keep the object URL stable otherwise.
 */
export function useLiveShot(enabled: boolean): LiveShotState {
  const [state, setState] = useState<LiveShotState>({
    entry: null,
    url: null,
    loading: enabled,
  });
  const entryRef = useRef<WallEntry | null>(null);
  const urlRef = useRef<string | null>(null);
  const emaRef = useRef(0);
  const coalescer = useRef(createCoalescer<WallEntry>());

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const revoke = (u: string | null) => {
      if (u !== null) URL.revokeObjectURL(u);
    };

    const poll = async () => {
      const t0 = performance.now();
      try {
        const res = await fetch('/api/app/surface_shots');
        const feed = (await res.json()) as { entries?: WallEntry[] };
        const next = feed.entries?.[0] ?? null;
        emaRef.current = nextEma(emaRef.current, performance.now() - t0);
        if (!stopped) {
          if (isUnchanged(entryRef.current, next)) {
            // unchanged screen — zero bytes, keep the old object URL
            setState((s) => ({ ...s, entry: next }));
          } else if (next !== null) {
            coalescer.current.schedule(next, async (entry) => {
              if (stopped) return;
              try {
                const img = await fetch(entry.thumbUrl);
                const blob = await img.blob();
                if (stopped) return;
                const url = URL.createObjectURL(blob);
                const old = urlRef.current;
                urlRef.current = url;
                entryRef.current = entry;
                setState({ entry, url, loading: false });
                revoke(old);
              } catch {
                // keep the previous frame on a failed fetch — an
                // honest "stale but present" beats a broken image
                entryRef.current = entry;
                setState((s) => ({ ...s, entry, loading: false }));
              }
            });
          }
        }
      } catch {
        // feed unreachable — retry at the adaptive cadence
      }
      if (!stopped) {
        timer = setTimeout(poll, computeNextPollMs(emaRef.current));
      }
    };

    poll();
    return () => {
      stopped = true;
      coalescer.current.cancel();
      if (timer !== null) clearTimeout(timer);
    };
  }, [enabled]);

  // release the object URL on unmount
  useEffect(
    () => () => {
      if (urlRef.current !== null) URL.revokeObjectURL(urlRef.current);
    },
    [],
  );

  return state;
}
