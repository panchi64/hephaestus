/**
 * The active project, as the client sends it — one `X-Heph-Project` header on every
 * REST call.
 *
 * Kept separate from both the API client and the projects store so neither has to
 * import the other (no cycle), exactly like `token.ts`: the store writes the
 * selection, the client reads it.
 *
 * This holds **no authority**. The backend resolves the operator's stored selection
 * on its own when the header is absent, so an unset value here is not "show nothing"
 * — it is "use whatever is active". The header exists so a surface can say something
 * the stored selection can't: `ALL` means *this request is deliberately unscoped*,
 * which is a different request from sending nothing at all.
 *
 * Mirrored to `localStorage` for the same reason the token is: a reload should not
 * silently drop the operator back into a different scope than the one they were
 * reading a moment ago. The backend is still the source of truth and re-resolves it.
 */

import { readLS, removeLS, writeLS } from "~/lib/storage";

const SCOPE_KEY = "heph.projects.active";

/** The literal the backend reads as "no filtering at all". */
export const ALL_PROJECTS = "all";

let inMemory: string | null = null;

export function getProjectScope(): string | null {
  return inMemory ?? readLS(SCOPE_KEY);
}

/** `null` clears the override so the backend falls back to the stored selection. */
export function setProjectScope(projectId: string | null): void {
  inMemory = projectId;
  if (projectId === null) removeLS(SCOPE_KEY);
  else writeLS(SCOPE_KEY, projectId);
}
