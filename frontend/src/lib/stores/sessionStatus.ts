/** The pure rule behind the session store: what the backend's vault state, plus
 *  whether we hold a token, means for what the operator should be shown.
 *
 *  Its own module for the reason `sendGate.ts` is: `session.ts` probes the backend
 *  at module load, so importing it to test four lines of derivation would mean
 *  starting a fetch. Nothing here touches the network or reactivity.
 */

export type SessionStatus = "loading" | "uninitialized" | "locked" | "unlocked";

/** `GET /auth/status`, as the backend sends it. */
export interface AuthStatus {
  initialized: boolean;
  unlocked: boolean;
  auth_enabled: boolean;
  /** The keyfile outlived its database — the operator cleared `app.db` expecting a
   *  reset and is being asked to unlock a key that now protects nothing. */
  db_missing: boolean;
  /** Whether the decrypted workspace (database and encrypted volume) is up. */
  workspace: "closed" | "opening" | "open" | "failed";
  /** Why the last open failed, such as no encrypted-volume backend on this host. */
  workspace_failure: string | null;
}

/** Map the backend's vault state (plus whether we hold a token) to our status. */
export function classify(s: AuthStatus, hasToken: boolean): SessionStatus {
  if (!s.initialized) return "uninitialized";
  // Unlocked but not yet open answers 423 to everything, so it is still locked to us.
  if (!s.unlocked || s.workspace !== "open") return "locked";
  if (!s.auth_enabled) return "unlocked"; // gate disabled — no token needed
  return hasToken ? "unlocked" : "locked";
}

/** Whether to warn that the workspace's key outlived its database.
 *
 *  Deliberately not a fifth `SessionStatus`. A wiped database does not change what
 *  the workspace *is* — it is still locked, and unlocking with the existing password
 *  still works and still lands somewhere usable (an empty workspace). It changes what
 *  the operator should be *told*, which is a different axis. And it is meaningless
 *  before setup: a fresh install has no key to have outlived anything. */
export function dbMissingFrom(s: AuthStatus | null): boolean {
  return s !== null && s.initialized && s.db_missing;
}
