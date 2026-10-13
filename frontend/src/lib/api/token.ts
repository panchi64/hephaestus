/**
 * The session bearer token — the single credential the client sends on every
 * request (including the SSE stream). Held in memory and mirrored to
 * `localStorage` so a reload survives without a re-login; the backend revalidates
 * it on the next call and the session store clears it on `401`/`423`.
 *
 * Kept separate from both the API client and the session store so neither has to
 * import the other (no cycle): the store writes the token, the client reads it.
 */

import { readLS, removeLS, writeLS } from "~/lib/storage";

const TOKEN_KEY = "heph.auth.token";

let inMemory: string | null = null;

export function getToken(): string | null {
  return inMemory ?? readLS(TOKEN_KEY);
}

export function setToken(token: string): void {
  inMemory = token;
  writeLS(TOKEN_KEY, token);
}

export function clearToken(): void {
  inMemory = null;
  removeLS(TOKEN_KEY);
}
