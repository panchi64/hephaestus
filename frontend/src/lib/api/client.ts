/**
 * The typed REST client — a thin `fetch` wrapper over the backend.
 *
 * Bearer auth on every request (the token from `./token`); JSON in/out; non-2xx
 * mapped to a typed {@link ApiError}. A `401`/`423` (unauthorized / vault locked)
 * clears the token and fires the registered expiry handler so the session store
 * can route back to login. This is the destination the feature `data.ts` seams
 * swap their mock bodies to.
 */
import { API_BASE } from "~/lib/config";
import { setBackendReachable } from "~/lib/stores/connectivity";
import { getProjectScope } from "./projectScope";
import { clearToken, getToken } from "./token";

/** `fetch` wrapped to echo backend reachability. A received response — even a 4xx/5xx —
 *  means the server answered, so the backend is reachable; only a transport-level failure
 *  (`fetch` rejects) flips it false, and a deliberate abort (per-call timeout) is not a
 *  connectivity signal. The platform-status derivation reads this to tint the favicon. */
async function trackedFetch(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    const res = await fetch(input, init);
    setBackendReachable(true);
    return res;
  } catch (err) {
    if (!(err instanceof DOMException && err.name === "AbortError")) {
      setBackendReachable(false);
    }
    throw err;
  }
}

export interface ApiError {
  status: number;
  detail: string;
  /** The input the backend blamed, when it said so (`detail: {field, message}`).
   *  Lets a form attach `detail` to the control it names instead of only
   *  toasting it. Absent for errors that name no field. */
  field?: string;
}

export function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    "detail" in value
  );
}

let onExpire: (() => void) | null = null;

/** Register what happens when the backend rejects our token (401/423). */
export function setExpireHandler(fn: () => void): void {
  onExpire = fn;
}

/** A `401`/`423` from anywhere — a REST call here, or the run SSE stream in
 *  `~/lib/stream/runStream` (which can't go through `request()`) — clears the
 *  stale token and fires the registered expiry handler so the session store
 *  routes back to login. The one place that reacts to a rejected token; reuse
 *  this rather than re-clearing the token and invoking the handler inline. */
export function handleAuthFailure(): void {
  clearToken();
  onExpire?.();
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...extra };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  // The one place the project scope is attached. It only ever *relays* the operator's
  // selection — the backend resolves its own stored active project when this is
  // absent, and decides what the scope means either way.
  const scope = getProjectScope();
  if (scope) headers["X-Heph-Project"] = scope;
  return headers;
}

/** The `{field, message}` object a route raises when it blames one input — the
 *  skills surface does, so its editor can attach the message to the control it
 *  names. Null for every other `detail` shape. */
function fieldDetail(body: unknown): { field: string; message: string } | null {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail))
    return null;
  const { field, message } = detail as { field?: unknown; message?: unknown };
  if (typeof field !== "string" || typeof message !== "string") return null;
  return { field, message };
}

/** FastAPI's own `RequestValidationError` handler (triggered by any request a
 *  Pydantic model rejects before the route body runs) sends `detail` as an array
 *  of `{loc, msg, type}` objects rather than the string our hand-raised
 *  `HTTPException`s use — and a field-scoped rejection sends a `{field, message}`
 *  object. Normalize all three shapes to a single readable string so no consumer
 *  has to special-case them. */
function normalizeDetail(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  const named = fieldDetail(body);
  if (named) return named.message;
  if (Array.isArray(detail) && detail.length > 0) {
    const messages = detail
      .map((entry) => {
        if (!entry || typeof entry !== "object") return null;
        const { loc, msg } = entry as { loc?: unknown; msg?: unknown };
        if (typeof msg !== "string") return null;
        const field = Array.isArray(loc) ? loc[loc.length - 1] : undefined;
        return field !== undefined && field !== null ? `${field}: ${msg}` : msg;
      })
      .filter((m): m is string => m !== null);
    if (messages.length > 0) return messages.join("; ");
  }
  return fallback;
}

async function toApiError(res: Response): Promise<ApiError> {
  let detail = res.statusText;
  let field: string | undefined;
  try {
    const body = await res.json();
    detail = normalizeDetail(body, detail);
    field = fieldDetail(body)?.field;
  } catch {
    /* non-JSON error body — keep the status text */
  }
  return { status: res.status, detail, field };
}

export interface RequestOptions {
  /** Abort the request when this signal fires (e.g. a per-call timeout). */
  signal?: AbortSignal;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts?: RequestOptions,
): Promise<T> {
  const headers = authHeaders();
  const init: RequestInit = {
    method,
    headers,
    credentials: "omit",
    signal: opts?.signal,
  };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await trackedFetch(`${API_BASE}${path}`, init);
  if (res.status === 401 || res.status === 423) handleAuthFailure();
  if (!res.ok) throw await toApiError(res);
  // Empty-body successes (204 No Content, 202 Accepted for async work) carry no JSON;
  // read text first and parse only when there's something to parse.
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** Send a multipart form (file upload). The browser sets the multipart
 *  Content-Type+boundary itself, so we must NOT set it here. Bearer auth and the
 *  401/423 handling match the JSON path. */
async function sendForm<T>(
  method: string,
  path: string,
  form: FormData,
): Promise<T> {
  const res = await trackedFetch(`${API_BASE}${path}`, {
    method,
    headers: authHeaders(),
    credentials: "omit",
    body: form,
  });
  if (res.status === 401 || res.status === 423) handleAuthFailure();
  if (!res.ok) throw await toApiError(res);
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) =>
    request<T>("GET", path, undefined, opts),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  del: <T = void>(path: string) => request<T>("DELETE", path),
  /** POST a multipart form (file upload). */
  postForm: <T>(path: string, form: FormData) =>
    sendForm<T>("POST", path, form),
  /** PUT a multipart form — the same upload, at a caller-chosen address (e.g. a
   *  skill bundle's file, whose path *is* its identity). */
  putForm: <T>(path: string, form: FormData) => sendForm<T>("PUT", path, form),
  /** The raw `Response` for an auth-gated GET, after the same auth handling and error
   *  mapping every other verb gets.
   *
   *  For the callers that need something off the *envelope* rather than the body — a
   *  content header the bytes cannot carry themselves. `getBlob` is this plus `.blob()`,
   *  rather than a second copy of the auth dance. */
  async getResponse(path: string): Promise<Response> {
    const res = await trackedFetch(`${API_BASE}${path}`, {
      headers: authHeaders(),
      credentials: "omit",
    });
    if (res.status === 401 || res.status === 423) handleAuthFailure();
    if (!res.ok) throw await toApiError(res);
    return res;
  },
  /** Fetch raw bytes (auth-gated content like artifacts) for a blob URL. */
  async getBlob(path: string): Promise<Blob> {
    return (await api.getResponse(path)).blob();
  },
};
