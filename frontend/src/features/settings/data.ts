import { createResource, createSignal, type Resource } from "solid-js";
import { api } from "~/lib/api";
import type { ReindexStatusDTO } from "~/lib/api/models-types";
import {
  refreshEndpoints,
  setEndpointEnabled,
  setRoleBinding as bindRole,
  setSelectedModel,
  testEndpoint,
  useEndpoints,
  useProviders,
  useRoles,
} from "~/lib/stores/models";
import {
  toChatSettings,
  toChatSettingsBody,
  type ChatSettingsDTO,
} from "./chatSettingsDto";
import type {
  AgentTool,
  ChatSettings,
  EmbeddingHealth,
  EndpointInput,
  OfflineState,
  ReindexStatus,
  SearchProvider,
  SearchProviderInput,
  VmState,
} from "./model";
import { toVmState, type VmStateDTO } from "./vmDto";

// The endpoint catalog, the provider presets, and the role bindings (reads AND
// the role writes) are owned by the shared models store so the chat picker,
// and Settings share one fetch and one type each; this module owns
// the endpoint CRUD writes. Store-owned actions are re-exported here so the
// screen reaches everything through this one seam — `setSelectedModel` included,
// because the Models page's CHAT card is the top-bar picker's control in another
// skin and must write the `main` binding through the same action it does.
export {
  useEndpoints,
  useProviders,
  useRoles,
  testEndpoint,
  setEndpointEnabled,
  setSelectedModel,
};

/** Map form values to the backend's snake_case body. `apiKey` undefined is
 *  omitted (leave unchanged); "" clears it. */
function toBody(input: Partial<EndpointInput>): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (input.name !== undefined) body.name = input.name;
  if (input.baseUrl !== undefined) body.base_url = input.baseUrl;
  if (input.provider !== undefined) body.provider = input.provider;
  if (input.model !== undefined) body.model = input.model;
  if (input.apiKey !== undefined) body.api_key = input.apiKey;
  if (input.contextWindow !== undefined)
    body.context_window = input.contextWindow;
  if (input.maxConcurrentRuns !== undefined)
    body.max_concurrent_runs = input.maxConcurrentRuns;
  if (input.nativeTools !== undefined) body.native_tools = input.nativeTools;
  if (input.vision !== undefined) body.vision = input.vision;
  if (input.thinking !== undefined) body.thinking = input.thinking;
  if (input.enabled !== undefined) body.enabled = input.enabled;
  return body;
}

/* ── Endpoints (writes) ───────────────────────────────────────────────────── */

/** Create an endpoint and return its backend id (the caller needs
 *  it to test + auto-select the new connection; the Settings form ignores it). */
export async function createEndpoint(input: EndpointInput): Promise<string> {
  const created = await api.post<{ id: string }>(
    "/models/endpoints",
    toBody(input),
  );
  refreshEndpoints();
  return created.id;
}

export async function updateEndpoint(
  id: string,
  patch: Partial<EndpointInput>,
): Promise<void> {
  await api.patch(`/models/endpoints/${id}`, toBody(patch));
  refreshEndpoints();
}

export async function deleteEndpoint(id: string): Promise<void> {
  await api.del(`/models/endpoints/${id}`);
  refreshEndpoints();
}

/* ── Role bindings ────────────────────────────────────────────────────────── */

/** Bind a role to an ordered chain (and, for `embedding`, a pinned model) via
 *  the store's single role-write action, then refresh this screen's embedding
 *  readouts. Errors are intentionally *not* swallowed — the backend rejects a
 *  non-embeddings model with a 422, and the caller surfaces that detail.
 *
 *  Returns whether the bind kicked off a background re-embed — only possible for
 *  the `embedding` role, and only when the endpoint/model actually changed — so
 *  the caller can acknowledge the work the operator just set in motion. */
export async function setRoleBinding(
  role: string,
  endpointIds: string[],
  model: string | null = null,
): Promise<boolean> {
  await bindRole(role, endpointIds, model);
  void refreshEmbeddingHealth(); // a bind can flip recall health
  // A changed embedding endpoint/model strands existing vectors, so the backend
  // heals them with a background re-embed. Pull the just-started status so the
  // live readout (and its poller) reflect it; report whether one kicked off.
  if (role !== "embedding") return false;
  await refreshReindexStatus();
  return reindexStatus()?.state === "running";
}

/* ── Embedding health + re-embed (reindex) ───────────────────────────────────
   These are LIVE-POLLED reads, so they must NOT go through `createResource` /
   Suspense: a refetching resource re-enters its pending state and re-triggers the
   route's `<Suspense>` fallback, flashing the whole page on every poll. Plain
   signals update in place — the readout ticks over without disrupting the screen. */

interface CapabilityDTO {
  key: string;
  status: string;
  detail: string;
}

const [embeddingHealth, setEmbeddingHealth] =
  createSignal<EmbeddingHealth | null>(null);

/** The backend owns the verdict on recall health — Settings only renders it (read
 *  off `/overview`, the home page's source of truth, not re-derived here). */
export function useEmbeddingHealth(): () => EmbeddingHealth | null {
  return embeddingHealth;
}

export async function refreshEmbeddingHealth(): Promise<void> {
  try {
    const o = await api.get<{ capabilities: CapabilityDTO[] }>("/overview");
    const cap = o.capabilities.find((c) => c.key === "embeddings");
    setEmbeddingHealth(
      cap
        ? {
            status: cap.status as EmbeddingHealth["status"],
            detail: cap.detail,
          }
        : null,
    );
  } catch {
    // Keep the last known health — a transient failure shouldn't blank the badge.
  }
}

function toReindexStatus(d: ReindexStatusDTO): ReindexStatus {
  return {
    state: d.state,
    memories: d.memories,
    messages: d.messages,
    detail: d.detail,
    completedAt: d.completed_at,
  };
}

const [reindexStatus, setReindexStatus] = createSignal<ReindexStatus | null>(
  null,
);

export function useReindexStatus(): () => ReindexStatus | null {
  return reindexStatus;
}

/** Poll the reindex status once (drives the live progress readout in place). */
export async function refreshReindexStatus(): Promise<void> {
  try {
    setReindexStatus(
      toReindexStatus(
        await api.get<ReindexStatusDTO>("/models/embedding/reindex"),
      ),
    );
  } catch {
    // Keep the last status on a transient poll failure.
  }
}

/** Manually re-embed memories + the chat index against the current embedding
 *  model (for a first index that failed, or to force a redo). The POST returns the
 *  freshly-started status, so the readout reflects it without a round-trip. */
export async function triggerReindex(): Promise<void> {
  setReindexStatus(
    toReindexStatus(
      await api.post<ReindexStatusDTO>("/models/embedding/reindex", {}),
    ),
  );
}

/* ── Web search providers ──────────────────────────────────────────────────── */

interface SearchProviderView {
  id: string;
  name: string;
  base_url: string;
  enabled: boolean;
  engines: string[];
  params: Record<string, unknown>;
  has_api_key: boolean;
}

/** The single snake_case→camel mapper for a provider row. */
function toSearchProvider(dto: SearchProviderView): SearchProvider {
  return {
    id: dto.id,
    name: dto.name,
    baseUrl: dto.base_url,
    enabled: dto.enabled,
    engines: dto.engines,
    params: dto.params,
    hasApiKey: dto.has_api_key,
  };
}

/** Map form values to the backend's snake_case body. `apiKey` undefined is
 *  omitted (leave unchanged); "" clears it. */
function toProviderBody(
  input: Partial<SearchProviderInput>,
): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (input.name !== undefined) body.name = input.name;
  if (input.baseUrl !== undefined) body.base_url = input.baseUrl;
  if (input.enabled !== undefined) body.enabled = input.enabled;
  if (input.engines !== undefined) body.engines = input.engines;
  if (input.params !== undefined) body.params = input.params;
  if (input.apiKey !== undefined) body.api_key = input.apiKey;
  return body;
}

const [providersTick, setProvidersTick] = createSignal(0);

async function fetchSearchProviders(): Promise<SearchProvider[]> {
  const rows = await api.get<SearchProviderView[]>("/search/providers");
  return rows.map(toSearchProvider);
}

export function useSearchProviders(): Resource<SearchProvider[]> {
  const [data] = createResource(providersTick, fetchSearchProviders);
  return data;
}

export async function createSearchProvider(
  input: SearchProviderInput,
): Promise<void> {
  await api.post("/search/providers", toProviderBody(input));
  setProvidersTick((n) => n + 1);
}

export async function updateSearchProvider(
  id: string,
  patch: Partial<SearchProviderInput>,
): Promise<void> {
  await api.patch(`/search/providers/${id}`, toProviderBody(patch));
  setProvidersTick((n) => n + 1);
}

export async function deleteSearchProvider(id: string): Promise<void> {
  await api.del(`/search/providers/${id}`);
  setProvidersTick((n) => n + 1);
}

/* ── Chat settings (turn budgets + conversation compaction) ──────────────────
   The two wire mappings live in `chatSettingsDto.ts`, not here: they are pure and
   worth pinning by a test, and this module reaches the network and the models store
   at import. */

export function useChatSettings(): Resource<ChatSettings> {
  const [data] = createResource(async () =>
    toChatSettings(await api.get<ChatSettingsDTO>("/chat/settings")),
  );
  return data;
}

/** Persist a subset of the chat preferences (an omitted field is left unchanged on the
 *  backend); returns the full stored settings. */
export async function saveChatSettings(
  patch: Partial<ChatSettings>,
): Promise<ChatSettings> {
  return toChatSettings(
    await api.put<ChatSettingsDTO>("/chat/settings", toChatSettingsBody(patch)),
  );
}

/* ── Offline mode ──────────────────────────────────────────────────────────────
   The backend can flip offline mode on its own when connectivity drops, so this is
   a LIVE-POLLED read (a plain signal, not a `createResource` — same reasoning as the
   reindex readout: a refetching resource would re-trigger the screen's Suspense
   fallback on every poll). The screen polls `refreshOfflineState` on a timer so an
   auto-toggle shows up without a reload. */

interface OfflineStateDTO {
  manual_offline: boolean;
  auto_detect: boolean;
  online: boolean;
  effective_offline: boolean;
}

/** The single snake_case→camel mapper for the offline state. */
function toOfflineState(dto: OfflineStateDTO): OfflineState {
  return {
    manualOffline: dto.manual_offline,
    autoDetect: dto.auto_detect,
    online: dto.online,
    effectiveOffline: dto.effective_offline,
  };
}

const [offlineState, setOfflineState] = createSignal<OfflineState | null>(null);

export function useOfflineState(): () => OfflineState | null {
  return offlineState;
}

/** Poll the offline state once (drives the live readout + toggles in place). */
export async function refreshOfflineState(): Promise<void> {
  try {
    setOfflineState(toOfflineState(await api.get<OfflineStateDTO>("/offline")));
  } catch {
    // Keep the last known state on a transient poll failure.
  }
}

/** Apply a switch change and reflect the fresh state the PUT returns (so the readout
 *  updates without a round-trip). */
async function putOffline(
  body: { manual_offline: boolean } | { auto_detect: boolean },
): Promise<void> {
  setOfflineState(
    toOfflineState(await api.put<OfflineStateDTO>("/offline", body)),
  );
}

/** Force offline mode on/off. */
export async function setOfflineManual(value: boolean): Promise<void> {
  await putOffline({ manual_offline: value });
}

/** Turn the auto-detect master switch on/off (does not itself force offline). */
export async function setOfflineAutoDetect(value: boolean): Promise<void> {
  await putOffline({ auto_detect: value });
}

/* ── Platform VM ───────────────────────────────────────────────────────────────
   A live-polled plain signal for the same reason as offline mode: the screen polls
   `refreshVmState` while the VM is starting. */

const [vmState, setVmState] = createSignal<VmState | null>(null);

export function useVmState(): () => VmState | null {
  return vmState;
}

export async function refreshVmState(): Promise<void> {
  try {
    setVmState(toVmState(await api.get<VmStateDTO>("/vm")));
  } catch {
    // Keep the last known state on a transient poll failure.
  }
}

/** Rebuild the platform VM and reflect the state the POST returns. */
export async function resetVm(): Promise<void> {
  setVmState(toVmState(await api.post<VmStateDTO>("/vm/reset")));
}

/* ── Agent tools ────────────────────────────────────────────────────────────────
   The catalog is the backend's — derived there from the live toolset registry, never
   enumerated here — so this is a plain read plus a per-tool flip. `/tools` is a
   snake_case surface whose field names are all single words, so the DTO and the model
   shape coincide and no mapper is needed. */

const [toolsTick, setToolsTick] = createSignal(0);

export function useAgentTools(): Resource<AgentTool[]> {
  const [data] = createResource(toolsTick, () =>
    api.get<AgentTool[]>("/tools"),
  );
  return data;
}

/** Enable or disable one tool for the agent. The backend re-reads the stored set when
 *  it composes each turn, so this applies from the next run onward — including the
 *  resume of a run that is currently parked awaiting approval. */
export async function setAgentToolEnabled(
  name: string,
  enabled: boolean,
): Promise<void> {
  await api.put(`/tools/${encodeURIComponent(name)}`, { enabled });
  setToolsTick((n) => n + 1);
}
