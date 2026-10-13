// ---------------------------------------------------------------------------
// Transfera v2 — Backend readiness flag (centralized)
// Single source of truth for "engine ready": health?.status === "ok".
// Mutation buttons consume this hook directly (no prop drilling) to grey
// out + tooltip while the engine is still starting. Navigation and
// already-cached views stay interactive — only backend-mutating actions
// gate on this. The api-client request interceptor enforces the same rule
// at the network layer for any call site this hook doesn't cover yet.
// ---------------------------------------------------------------------------

import { useHealth } from "@/lib/queries";

/** Tooltip shown on gated mutation buttons while the engine starts. */
export const ENGINE_WAITING_LABEL = "Waiting for engine…";

/**
 * True once the backend health endpoint reports status "ok".
 * React Query keeps last-known data across background refetches, so this
 * only reads false during cold start / genuine outage — never flickers
 * during normal polling.
 */
export function useBackendReady(): boolean {
  const { data: health } = useHealth();
  return health?.status === "ok";
}
