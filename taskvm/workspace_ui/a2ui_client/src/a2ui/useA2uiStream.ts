/**
 * useA2uiStream — the island's ONLY A2UI ingestion point.
 *
 * Wraps the official `MessageProcessor` from `@a2ui/web_core/v0_9`:
 * feeds it ordered `A2uiMessage[]` (mock now, SSE later — same shape),
 * keeps a reactive list of live surfaces, and funnels every renderer
 * action through the ActionListener bridge (structured events, never
 * re-translated free text).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MessageProcessor } from '@a2ui/web_core/v0_9';
import { basicCatalog, type ReactComponentImplementation } from '@a2ui/react/v0_9';
import type { Catalog, SurfaceModel, A2uiClientAction } from '@a2ui/web_core/v0_9';
import { PROTOCOL_VERSION, type A2uiMessage } from './protocol';

export type A2uiActionListener = (action: A2uiClientAction) => void;

export interface A2uiStream {
  /** Live surfaces in creation order (React-ready snapshots). */
  surfaces: SurfaceModel<ReactComponentImplementation>[];
  /** Feed ordered protocol messages (idempotent per mount). */
  processMessages: (messages: A2uiMessage[]) => void;
  /** Clear every surface (new session / demo reset). */
  reset: () => void;
  /** The underlying processor (escape hatch for advanced wiring). */
  processor: MessageProcessor<ReactComponentImplementation>;
}

export function useA2uiStream(onAction?: A2uiActionListener): A2uiStream {
  const actionRef = useRef(onAction);
  actionRef.current = onAction;

  const processor = useMemo(() => {
    const catalogs: Catalog<ReactComponentImplementation>[] = [basicCatalog];
    return new MessageProcessor<ReactComponentImplementation>(
      catalogs,
      (action) => actionRef.current?.(action),
      { version: PROTOCOL_VERSION },
    );
  }, []);

  const [surfaces, setSurfaces] = useState<
    SurfaceModel<ReactComponentImplementation>[]
  >(() => Array.from(processor.model.surfacesMap.values()));

  useEffect(() => {
    const sync = () =>
      setSurfaces(Array.from(processor.model.surfacesMap.values()));
    const created = processor.onSurfaceCreated(sync);
    const deleted = processor.onSurfaceDeleted(sync);
    return () => {
      created.unsubscribe();
      deleted.unsubscribe();
    };
  }, [processor]);

  const processMessages = useCallback(
    (messages: A2uiMessage[]) => {
      processor.processMessages(messages);
      // processMessages is synchronous; sync once more in case a
      // listener batch was missed (defensive, keeps hook contract simple).
      setSurfaces(Array.from(processor.model.surfacesMap.values()));
    },
    [processor],
  );

  const reset = useCallback(() => {
    for (const id of Array.from(processor.model.surfacesMap.keys())) {
      processor.processMessages([
        { version: PROTOCOL_VERSION, deleteSurface: { surfaceId: id } },
      ]);
    }
    setSurfaces([]);
  }, [processor]);

  return { surfaces, processMessages, reset, processor };
}
