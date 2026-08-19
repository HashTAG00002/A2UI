/**
 * protocol — the TypeScript-side mirror of `taskvm/genui/protocol.py`.
 *
 * Single source of truth for the identity the CLIENT needs: protocol
 * version, catalog id, surface-id rule, the one allowed dynamic-surface
 * action, and the governance action names that only the fixed shell
 * owns. Keep in sync with the Python side (tests cross-check the mock
 * stream's shape against the real SDK validator on the backend).
 */
export const PROTOCOL_VERSION = 'v0.9' as const;

export const CATALOG_ID =
  'https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json' as const;

/** `taskvm-task-<sanitized-session-id>` — same rule as the backend. */
export function surfaceIdForSession(sessionId: string): string {
  const cleaned = sessionId
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '');
  if (!cleaned) throw new Error('sessionId must be non-empty');
  return `taskvm-task-${cleaned}`;
}

/** The ONLY action a model-generated dynamic surface may emit. */
export const ACTION_LOCAL_PATCH = 'taskvm.local_patch' as const;

export type A2uiMessage =
  | { version: typeof PROTOCOL_VERSION; createSurface: { surfaceId: string; catalogId: string } }
  | { version: typeof PROTOCOL_VERSION; updateComponents: { surfaceId: string; components: unknown[] } }
  | { version: typeof PROTOCOL_VERSION; updateDataModel: { surfaceId: string; path?: string; value?: unknown } }
  | { version: typeof PROTOCOL_VERSION; deleteSurface: { surfaceId: string } };

export function createSurfaceMessage(surfaceId: string): A2uiMessage {
  return {
    version: PROTOCOL_VERSION,
    createSurface: { surfaceId, catalogId: CATALOG_ID },
  };
}

export function updateComponentsMessage(
  surfaceId: string,
  components: unknown[],
): A2uiMessage {
  return { version: PROTOCOL_VERSION, updateComponents: { surfaceId, components } };
}

export function updateDataModelMessage(
  surfaceId: string,
  value: unknown,
  path = '/',
): A2uiMessage {
  return { version: PROTOCOL_VERSION, updateDataModel: { surfaceId, path, value } };
}
