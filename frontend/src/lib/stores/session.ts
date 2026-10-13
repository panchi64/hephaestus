import { createSignal } from "solid-js";
import {
  api,
  clearToken,
  getToken,
  isApiError,
  setExpireHandler,
  setToken,
} from "~/lib/api";
import {
  classify,
  dbMissingFrom,
  type AuthStatus,
  type SessionStatus,
} from "./sessionStatus";

export type { SessionStatus };

/**
 * Session store — the real auth state for the single-operator backend.
 *
 * Hephaestus has one operator and a password-derived, memory-only vault key
 * (lock-until-unlocked). There is no multi-user model, no privilege tiers — the
 * only question is whether the workspace is *unlocked* and we hold a valid token.
 *
 * The seam: screens/guards read `useSession()`; the store owns the bearer token
 * (via `~/lib/api/token`) and the backend `/auth/*` calls. A `401`/`423` from any
 * request flips us back to `locked` through the client's expiry handler.
 */

interface TokenResponse {
  token: string;
}

/** What a workspace reset actually removed, as the backend reports it. */
export interface ResetSummary {
  removed: string[];
  bytesFreed: number;
  failed: string[];
}

interface ResetSummaryDTO {
  removed: string[];
  bytes_freed: number;
  failed: string[];
}

const [status, setStatus] = createSignal<SessionStatus>("loading");
const [dbMissing, setDbMissing] = createSignal(false);

/** The probe's own failure, when the backend answered but answered badly. Held so
 *  the unlock screen can say *that* instead of presenting a plain locked vault — a
 *  500 from a half-wiped data directory is not a workspace waiting for a password. */
const [probeError, setProbeError] = createSignal("");

/** How long one probe may hang before we abandon it. A backend that has bound its
 *  listening socket but hasn't finished starting — exactly what uvicorn's reloader
 *  does, and what the dev server races on a cold boot — *accepts* the connection
 *  and never answers, so a probe with no deadline never settles. That, not a
 *  refused connection, is what used to strand the app on "ESTABLISHING LINK…"
 *  until the operator reloaded the page by hand. */
const PROBE_TIMEOUT_MS = 2000;

/** Backoff between boot re-probes, one entry per retry. Bounded: once it runs out
 *  we settle on `locked`, so a genuinely dead backend lands on the login screen
 *  (which can retry on submit) rather than a splash that never resolves. */
const PROBE_BACKOFF_MS = [250, 500, 1000, 2000];

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** One probe attempt, in three outcomes: the backend's answer; `"errored"` when it
 *  answered but badly (it's up, just unhappy — nothing to wait for); or `null` when
 *  it never answered at all, the transport failure worth waiting out. */
type Probe = AuthStatus | "errored" | null;

async function probe(): Promise<Probe> {
  try {
    const answer = await api.get<AuthStatus>("/auth/status", {
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    setProbeError("");
    return answer;
  } catch (err) {
    if (!isApiError(err)) return null;
    // An HTTP error settles us on the unlock screen, because that is the surface
    // that can retry — but it is not evidence of a locked vault, so the reason is
    // carried through rather than dressed up as one.
    setProbeError(err.detail);
    return "errored";
  }
}

function apply(result: Probe): SessionStatus {
  // No usable answer — present as locked so the unlock screen can retry, and claim
  // nothing about a workspace the backend never described.
  const answer = typeof result === "object" ? result : null;
  const next = answer ? classify(answer, getToken() !== null) : "locked";
  if (next !== "unlocked") clearToken(); // a stale token can't unlock us
  setDbMissing(dbMissingFrom(answer));
  setStatus(next);
  return next;
}

/** Probe the backend for the current vault state. */
export async function refresh(): Promise<SessionStatus> {
  return apply(await probe());
}

/** The boot probe. The page is routinely up before the backend is, so a single
 *  attempt is a coin flip: retry on a bounded backoff and only then fall back to
 *  `refresh()`'s unreachable handling. The status stays `loading` across the
 *  retries — flashing the login screen at a backend that's merely still starting
 *  would be a worse lie than the splash. */
async function boot(): Promise<void> {
  for (const delay of PROBE_BACKOFF_MS) {
    const next = await probe();
    if (next !== null) {
      apply(next);
      return;
    }
    await sleep(delay);
  }
  await refresh();
}

/** First-run: choose the operator password and unlock. */
export async function setup(password: string): Promise<void> {
  const { token } = await api.post<TokenResponse>("/setup", { password });
  setToken(token);
  setStatus("unlocked");
}

/** Abandon a workspace whose database is gone: the backend removes the encryption
 *  key and everything sealed under it, which leaves the workspace uninitialized and
 *  the gate showing setup. Re-probe rather than assuming that — the backend reports
 *  what it managed to delete, and the state that follows is its call, not ours. */
export async function resetWorkspace(): Promise<ResetSummary> {
  const dto = await api.post<ResetSummaryDTO>("/setup/reset");
  clearToken();
  await refresh();
  return {
    removed: dto.removed,
    bytesFreed: dto.bytes_freed,
    failed: dto.failed,
  };
}

/** Unlock the workspace with the operator password. */
export async function unlock(password: string): Promise<void> {
  const { token } = await api.post<TokenResponse>("/auth/login", { password });
  setToken(token);
  setStatus("unlocked");
}

/** Drop our session token (the vault stays unlocked server-side). */
export async function logout(): Promise<void> {
  try {
    await api.post("/auth/logout");
  } catch {
    /* best effort — clear locally regardless */
  }
  clearToken();
  setStatus("locked");
}

/** Wipe the vault key from the backend's memory and end all sessions.
 *
 *  The local half is unconditional, as it always was: whatever the backend did or
 *  didn't manage, this client is done holding a token. What changed is that the
 *  failure is no longer swallowed. LOCK is the one control in the app whose whole
 *  promise is about what happens *after* the operator walks away, and a locked-looking
 *  screen over a vault key still resident in the backend's memory is precisely the
 *  state they must not be left believing in. The caller surfaces it. */
export async function lock(): Promise<void> {
  let failure: unknown = null;
  try {
    await api.post("/auth/lock");
  } catch (err) {
    failure = err;
  }
  clearToken();
  setStatus("locked");
  if (failure) throw failure;
}

// A rejected token (expired session / re-locked vault) returns us to locked.
setExpireHandler(() => setStatus("locked"));

// Probe vault state on load (client-only SPA), retrying while the backend comes up.
void boot();

export function useSession() {
  return {
    get status(): SessionStatus {
      return status();
    },
    get isAuthenticated(): boolean {
      return status() === "unlocked";
    },
    /** The workspace's key outlived its database. Still `locked` — unlocking works
     *  and lands in an empty workspace — but the operator deserves to be told. */
    get dbMissing(): boolean {
      return dbMissing();
    },
    /** Why the status probe failed, when the backend answered with an error rather
     *  than describing a workspace. Empty when it answered normally. */
    get probeError(): string {
      return probeError();
    },
    refresh,
    setup,
    unlock,
    logout,
    lock,
    resetWorkspace,
  };
}
