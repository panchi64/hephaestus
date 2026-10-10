/** Settings feature data contracts.
 *
 * The connected surface is model configuration: the backend's endpoint registry
 * (`/models/endpoints`) and the role→endpoint bindings (`/models/roles`), plus the
 * web-search provider registry (`/search/providers`). There is no
 * user-preferences/2FA/account model — Odysseus is single-operator. The endpoint
 * read shape (`ModelEndpoint`) is owned by `~/lib/stores/models`, shared with the
 * chat picker; this module holds the write/form and role contracts, and the
 * search-provider shapes (which only Settings reads — the agent reaches search
 * through its tool, not the frontend). */

import type { ReindexState } from "~/lib/api/models-types";

/** Form values for creating/updating an endpoint. `apiKey` omitted = unchanged,
 *  `apiKey: ""` clears the key; `model: ""` clears the default model. */
export interface EndpointInput {
  name: string;
  baseUrl: string;
  /** The provider adapter id (from `GET /models/providers`); omitted on create ⇒
   *  the backend's default ("openai-compatible"). */
  provider?: string;
  model?: string;
  apiKey?: string;
  contextWindow: number | null;
  nativeTools: boolean;
  vision: boolean;
  thinking: boolean;
  /** Whether the endpoint is active. Omitted from the create/edit form (which
   *  defaults it on the backend); carried so the row toggle can PATCH it. */
  enabled?: boolean;
}

/** The roles that resolve against an ordered fallback chain — the Models pane's
 *  ENDPOINTS tab (`FallbackChainsSection`) is what orders them. `main` is
 *  absent because it is single-endpoint: its card overwrites the whole binding,
 *  exactly as the top-bar picker does. The binding shape
 *  (`RoleBinding`/`RoleBindings`) is owned by `~/lib/stores/models`. */
export const BINDABLE_ROLES = ["utility", "embedding"] as const;

/** Progress of a background re-embed (after the embedding model changes). The
 *  vectors of every memory + chat message are re-embedded into the new model's
 *  space; until it finishes, semantic recall is partially degraded. */
export interface ReindexStatus {
  state: ReindexState;
  memories: number;
  messages: number;
  detail: string | null;
  completedAt: string | null;
}

/** The backend's authoritative read on whether semantic recall is healthy:
 *  `nominal` (hybrid recall) or `warn` (keyword-only — no/!ready embedder). */
export interface EmbeddingHealth {
  status: "nominal" | "warn" | "alert";
  detail: string;
}

/* ── Web search providers ──────────────────────────────────────────────────── */

/** The operator's view of a configured search provider (a SearXNG instance). The
 *  agent's web search queries the first `enabled` one; the rest stay configured as
 *  alternates. The API key is write-only — only its presence is exposed. */
export interface SearchProvider {
  id: string;
  name: string;
  baseUrl: string;
  enabled: boolean;
  /** Optional SearXNG engine filter (e.g. ["google", "duckduckgo"]); [] ⇒ default. */
  engines: string[];
  /** Extra query params passed through verbatim (e.g. {"language": "en"}). */
  params: Record<string, unknown>;
  /** Whether a key is stored — the value is write-only and never returned. */
  hasApiKey: boolean;
}

/** Form values for creating/updating a provider. `apiKey` omitted = unchanged,
 *  `apiKey: ""` clears the key. */
export interface SearchProviderInput {
  name: string;
  baseUrl: string;
  enabled: boolean;
  engines: string[];
  params: Record<string, unknown>;
  apiKey?: string;
}

/** Operator-tunable chat preferences. The `autoCompact*` fields tune conversation
 *  compaction — folding whole earlier *turns* into a utility-model summary once the
 *  context window fills: whether it's on, how full the window must get first
 *  (`autoCompactThreshold` is a fraction of the window, the same 0–1 quantity the
 *  context meter reports; the UI presents it as a percentage). A fold keeps nothing
 *  verbatim — the summary alone carries the thread. It is the only reduction there
 *  is — per-tool-result digesting was removed.
 *  `agentRequestLimit` is how many model round-trips a single turn may spend before it
 *  stops — the ceiling a long tool-using turn actually runs out of.
 *  `inactivityTimeoutS` is how long (seconds) a run may go without emitting an event
 *  before the watchdog stops it — the bound a long generation (a big write, a slow
 *  first token) needs raised to stay alive.
 *  `wallClockTimeoutS` is a total-duration bound on a run however busy it is, and is
 *  `null` (no bound) unless the operator sets one: a turn is already bounded by
 *  `agentRequestLimit`, so a wall clock mostly stops runs that are merely slow. What it
 *  does catch is a run that keeps emitting — a tool streaming progress, a model streaming
 *  tokens — and so refreshes the inactivity watchdog forever without spending a step.
 *  `contextWarnThreshold`/`contextAlertThreshold` are where the composer's context gauge
 *  turns amber and then red — fractions like `autoCompactThreshold`, and tunable for the
 *  same reason the ring is grey below them: how much remaining room counts as "enough"
 *  depends on how long the operator's turns are, not on the model. `warn` is always
 *  strictly below `alert`; the backend refuses a pair that isn't.
 *  `subagentMaxConcurrent` is how many sub-agents the agent may have running at once, and
 *  is `null` (no cap) unless the operator sets one — the second field here where `null` is
 *  a value rather than an absence. It bounds how many the agent may *have*, which is about
 *  the operator's attention and their bill; how many actually execute together is a host
 *  bound the backend keeps separately. */
export interface ChatSettings {
  autoCompactEnabled: boolean;
  autoCompactThreshold: number;
  /** How long a thread must sit untouched before the backend writes its work
   *  summary — the paragraph the thread-recap band shows on re-entry. Minutes. */
  workSummaryIdleMinutes: number;
  contextWarnThreshold: number;
  contextAlertThreshold: number;
  agentRequestLimit: number;
  inactivityTimeoutS: number;
  wallClockTimeoutS: number | null;
  subagentMaxConcurrent: number | null;
}

/** What `subagentMaxConcurrent` seeds to when it is switched on and nothing was stored.
 *  The frontend's own suggestion, like `DEFAULT_WALL_CLOCK_S` and for the same reason:
 *  there is no backend default to borrow, because the cap is absent until asked for. */
export const DEFAULT_SUBAGENT_LIMIT = 3;

/** What `wallClockTimeoutS` seeds to when it is switched on and nothing was stored — the
 *  frontend's own starting suggestion, not a backend default (there isn't one: the bound
 *  is absent until an operator asks for it). */
export const DEFAULT_WALL_CLOCK_S = 1800;

/** `wallClockTimeoutS` as the whole minutes the Chat panel offers. The caller keeps its
 *  own policy for "there is no bound", so this takes a real duration. */
export function wallClockMinutes(seconds: number): number {
  return Math.max(1, Math.round(seconds / 60));
}

/* ── Offline mode ──────────────────────────────────────────────────────────── */

/** The backend's read on offline mode (connectivity-aware web-container suspension).
 *  The backend owns every decision here — the frontend only renders this and relays
 *  the two switches back. `manualOffline` forces offline; `autoDetect` lets the
 *  connectivity monitor toggle it; `online` is the raw connectivity verdict; and
 *  `effectiveOffline` (= manual OR (auto AND NOT online)) is what's actually in
 *  effect — the web containers are down and the agent's web tools are hidden. */
export interface OfflineState {
  manualOffline: boolean;
  autoDetect: boolean;
  online: boolean;
  effectiveOffline: boolean;
}

/* ── Agent tools ───────────────────────────────────────────────────────────── */

/** One tool in the agent's catalog, as the backend reports it. The catalog is
 *  derived from the live toolset registry — the frontend never enumerates tools of
 *  its own. `name` is the namespaced `category_tool` name the agent is offered;
 *  `enabled: false` means the operator switched it off, and the backend then withholds
 *  it from every run (`AE-3.3`). Offline mode can withhold the web tools on top of
 *  this without changing `enabled` — that's the backend's call, not a second state
 *  the operator sets. */
export interface AgentTool {
  name: string;
  category: string;
  description: string;
  enabled: boolean;
  /** The backend holds this tool's group back from the opening tool list and hands the
   *  agent a one-line index instead, so the group costs nothing on a turn that never
   *  needs it. **Orthogonal to `enabled`** — a dormant tool is on, and the operator has
   *  no switch for this: which groups start dormant is the backend's assembly decision,
   *  and this flag is here so a row that is quietly absent from a turn doesn't read as
   *  a row that was switched off. */
  dormant: boolean;
}
