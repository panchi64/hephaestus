/** Wire types for the models domain — the backend `/models/*` contract.
 *
 *  One module holds every snake_case DTO and string-literal union the models
 *  surface speaks, so a backend rename is fixed in exactly one place and no two
 *  seams can drift apart. Feature `data.ts` files and the models store import
 *  from here and map to their own camelCase view types; nothing re-declares
 *  these shapes locally. */

/* ── Endpoint health tokens ─────────────────────────────────────────────────── */

/** The verdict of the last reachability probe (null until first tested). The
 *  backend owns these tokens; the frontend renders them, never re-categorizes. */
export type EndpointStatus = "ok" | "error" | "untested";
export type EndpointErrorCategory =
  | "ok"
  | "auth"
  | "rate_limited"
  | "timeout"
  | "unreachable"
  | "bad_response"
  | "server_error";

/* ── Endpoints ──────────────────────────────────────────────────────────────── */

/** One configured endpoint, as `GET /models/endpoints` returns it. */
export interface EndpointViewDTO {
  id: string;
  name: string;
  /** The provider adapter typing this endpoint (e.g. "openai-compatible",
   *  "anthropic", "google", "local"). */
  provider: string;
  base_url: string;
  model: string | null;
  has_api_key: boolean;
  context_window: number | null;
  max_concurrent_runs: number | null;
  native_tools: boolean;
  vision: boolean;
  thinking: boolean;
  enabled: boolean;
  last_status: EndpointStatus | null;
  last_error_category: EndpointErrorCategory | null;
  last_error_detail: string | null;
  last_checked_at: string | null;
}

/** `POST /models/endpoints/{id}/test` — the persisted probe verdict. */
export interface EndpointTestDTO {
  status: "ok" | "error";
  error_category: EndpointErrorCategory;
  error_detail: string;
  checked_at: string;
}

/** `GET /models/endpoints/{id}/models` — runtime model discovery. */
export interface EndpointModelsDTO {
  models: string[];
  supported: boolean;
}

/* ── Providers ──────────────────────────────────────────────────────────────── */

/** One provider adapter's preset, as `GET /models/providers` returns it — what
 *  the endpoint editor / guided setup prefills from, so the frontend never
 *  hardcodes a lab's details. */
export interface ProviderViewDTO {
  id: string;
  display_name: string;
  requires_key: boolean;
  default_base_url: string | null;
  key_hint: string | null;
  docs_url: string | null;
  native_tools: boolean;
  vision: boolean;
}

/* ── Roles ──────────────────────────────────────────────────────────────────── */

/** One role's binding, as `GET /models/roles` returns it (and as
 *  `PUT /models/roles/{role}` accepts it). */
export interface RoleViewDTO {
  endpoint_ids: string[];
  model: string | null;
  context_window: number | null;
  /** True when the backend picked this rather than the operator: `main` with nothing
   *  bound resolves to the first usable endpoint/model and reports what it resolved
   *  to. A default, not a pin — it moves when a better endpoint appears. */
  implicit: boolean;
}

/* ── Embedding reindex ──────────────────────────────────────────────────────── */

export type ReindexState = "idle" | "running" | "done" | "degraded" | "error";

/** `GET`/`POST /models/embedding/reindex` — the background re-embed job. */
export interface ReindexStatusDTO {
  state: ReindexState;
  memories: number;
  messages: number;
  detail: string | null;
  completed_at: string | null;
}
