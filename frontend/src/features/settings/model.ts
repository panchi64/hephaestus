/** Settings feature data contracts: endpoint registry (`/models/endpoints`), role
 * bindings (`/models/roles`) and search providers (`/search/providers`). The endpoint
 * read shape (`ModelEndpoint`) lives in `~/lib/stores/models`, shared with the chat
 * picker. */

import type { ReindexState } from "~/lib/api/models-types";

/** Form values for creating/updating an endpoint. `apiKey` omitted = unchanged,
 *  `apiKey: ""` clears the key; `model: ""` clears the default model. */
export interface EndpointInput {
  name: string;
  baseUrl: string;
  /** Adapter id from `GET /models/providers`; omitted on create uses "openai-compatible". */
  provider?: string;
  model?: string;
  apiKey?: string;
  contextWindow: number | null;
  /** Null on create is automatic; on update 0 clears a stored limit back to automatic
   *  and omitting it leaves the limit as it was. */
  maxConcurrentRuns?: number | null;
  nativeTools: boolean;
  vision: boolean;
  thinking: boolean;
  /** Omitted from the create/edit form; carried for the row toggle's PATCH. */
  enabled?: boolean;
}

/** Roles that resolve against an ordered fallback chain (ordered in
 *  `FallbackChainsSection`). `main` is absent: it is single-endpoint. `decision` is
 *  absent: only the head of its chain is asked. The binding shape
 *  (`RoleBinding`/`RoleBindings`) lives in `~/lib/stores/models`. */
export const BINDABLE_ROLES = ["utility", "embedding"] as const;

/** Progress of the background re-embed of every memory and chat message after the
 *  embedding model changes; semantic recall is degraded until it finishes. */
export interface ReindexStatus {
  state: ReindexState;
  memories: number;
  messages: number;
  detail: string | null;
  completedAt: string | null;
}

/** A capability row from `/overview`. For recall, `warn` is keyword-only (no ready
 *  embedder); for web search, `warn` is no usable instance (offline, or the built-in
 *  one not up). */
export interface CapabilityHealth {
  status: "nominal" | "warn" | "alert";
  detail: string;
}

export interface WebSearchReadout {
  /** Which instance answers: "built-in" or "own". */
  source: "built-in" | "own";
  label: string;
  tone: "nominal" | "warn" | "dim";
}

/** What web search runs on. An enabled operator instance overrides the built-in
 *  SearXNG; a non-nominal health shows the backend's reason (starting, offline…). */
export function webSearchReadout(
  health: CapabilityHealth | null,
  ownEnabled: number,
): WebSearchReadout {
  const source = ownEnabled > 0 ? "own" : "built-in";
  if (!health) return { source, label: "Checking", tone: "dim" };
  if (health.status !== "nominal")
    return { source, label: health.detail, tone: "warn" };
  return {
    source,
    label: source === "own" ? "Your instance" : "Built in",
    tone: "nominal",
  };
}

/** An operator-configured SearXNG instance. The first `enabled` one overrides the
 *  built-in instance. */
export interface SearchProvider {
  id: string;
  name: string;
  baseUrl: string;
  enabled: boolean;
  /** SearXNG engine filter (e.g. ["google", "duckduckgo"]); [] uses the default. */
  engines: string[];
  /** Extra query params passed through verbatim. */
  params: Record<string, unknown>;
  /** The key itself is write-only and never returned. */
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

/** Operator-tunable chat preferences.
 *  `autoCompactThreshold` is the fraction (0-1, as the context meter reports) of the
 *  window that triggers folding earlier turns into a utility-model summary.
 *  `inactivityTimeoutS` is how long (seconds) a run may emit nothing before the watchdog
 *  stops it.
 *  `wallClockTimeoutS` bounds total run duration; `null` means no bound. It catches runs
 *  that keep emitting and so keep refreshing the inactivity watchdog.
 *  `contextWarnThreshold`/`contextAlertThreshold` are the fractions where the composer's
 *  context gauge turns amber and red; `warn` is strictly below `alert` (the backend
 *  refuses otherwise).
 *  `subagentMaxConcurrent` caps how many sub-agents the agent may have; `null` means the
 *  backend default of 8. Host-side execution concurrency is bounded separately. */
export interface ChatSettings {
  autoCompactEnabled: boolean;
  autoCompactThreshold: number;
  /** Minutes a thread sits untouched before the backend writes the work summary shown
   *  by the thread-recap band. */
  workSummaryIdleMinutes: number;
  contextWarnThreshold: number;
  contextAlertThreshold: number;
  /** Seconds a turn waits for the first token; `null` leaves that wait to the
   *  inactivity watchdog. */
  firstTokenTimeoutS: number | null;
  inactivityTimeoutS: number;
  wallClockTimeoutS: number | null;
  subagentMaxConcurrent: number | null;
}

/** Seed for `subagentMaxConcurrent` when switched on with nothing stored; matches the backend default. */
export const DEFAULT_SUBAGENT_LIMIT = 8;

/** Seed for `wallClockTimeoutS` when switched on; a frontend suggestion, the backend has no default. */
export const DEFAULT_WALL_CLOCK_S = 1800;

/** `wallClockTimeoutS` as whole minutes for the Chat panel; takes a real duration, not "no bound". */
export function wallClockMinutes(seconds: number): number {
  return Math.max(1, Math.round(seconds / 60));
}

/** Offline mode, decided by the backend. `effectiveOffline` is
 *  manual OR (auto AND NOT online): web services are down and the agent's web tools hidden. */
export interface OfflineState {
  manualOffline: boolean;
  autoDetect: boolean;
  online: boolean;
  effectiveOffline: boolean;
}

export type VmStatus = "ready" | "starting" | "unavailable";

/** The services VM that runs agent code, previews, search and web fetch. */
export interface VmState {
  state: VmStatus;
  detail: string | null;
}

/** One tool in the agent's catalog, from the live toolset registry. `name` is the
 *  namespaced `category_tool` name; `enabled: false` withholds it from every run.
 *  Offline mode can withhold web tools without changing `enabled`. */
export interface AgentTool {
  name: string;
  category: string;
  description: string;
  enabled: boolean;
  /** The group is held back from the opening tool list behind a one-line index.
   *  Orthogonal to `enabled`: a dormant tool is on, and the operator has no switch for it. */
  dormant: boolean;
}
