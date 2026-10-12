/** Global model state — the endpoint catalog, the provider presets, runtime
 *  model discovery, and the role bindings (including the chat `main` selection).
 *  One place owns all of it so the app shell's top-bar picker, the overview
 *  launchpad, chat, compare, and Settings share a single source of truth
 *  (one `/models/endpoints` fetch, one `/models/roles` fetch) — and every role
 *  write goes through the store's actions, never a feature-local PUT.
 *
 *  An *endpoint* is a provider connection; its models are discovered at runtime
 *  from the provider (`GET /models/endpoints/{id}/models`). The chat model *is* the
 *  backend `main` role binding: the picker reads it from `/models/roles` and writes
 *  it back with `PUT /models/roles/main` (single endpoint + pinned model), so the
 *  same fact every server-initiated consumer resolves (research, tasks, titling) is
 *  the one the operator picked — no device-local authoritative state. The selection
 *  is held structured (`{endpointId, model}`); the `endpointId::model` composite
 *  exists only at the picker boundary (the Combobox needs one string per option),
 *  so a model id containing `::` never round-trips. The Settings ROLE BINDINGS panel
 *  writes the same binding — one source, two UIs. */

import {
  createComputed,
  createMemo,
  createResource,
  createRoot,
  createSignal,
  type Resource,
} from "solid-js";
import { api } from "~/lib/api";
import type {
  EndpointModelsDTO,
  EndpointStatus,
  EndpointErrorCategory,
  EndpointTestDTO,
  EndpointViewDTO,
  ProviderViewDTO,
  RoleViewDTO,
} from "~/lib/api/models-types";
import { settled } from "~/lib/resource";
import { useSession } from "~/lib/stores/session";
import { sendBlocker } from "./sendGate";

/** A specific model on a specific endpoint — the unit of selection. */
export interface ModelSelection {
  endpointId: string;
  model: string;
}

/** The operator's view of a configured endpoint (the shared read shape). */
export interface ModelEndpoint {
  id: string;
  name: string;
  /** The provider adapter typing this endpoint (e.g. "openai-compatible",
   *  "anthropic", "google", "local"). */
  provider: string;
  baseUrl: string;
  /** Default/fallback model. Null when models are discovered dynamically and no
   *  default was set. */
  model: string | null;
  /** Whether a key is stored — the value is write-only and never returned. */
  hasApiKey: boolean;
  /** The operator's *override*, null on every endpoint that never needed one. Only the
   *  Settings form should read this — it is what belongs in the input box, and it is
   *  not the window the endpoint runs under. That belongs to the binding, which is
   *  where the model is decided: see `RoleBinding.contextWindow`. */
  contextWindow: number | null;
  /** How many runs may use this endpoint at once; null is automatic — the server's
   *  slot count when it reports one, otherwise unlimited. */
  maxConcurrentRuns: number | null;
  nativeTools: boolean;
  vision: boolean;
  thinking: boolean;
  /** Whether this endpoint is active — disabled endpoints are hidden from the
   *  picker and skipped in fallback chains (the backend enforces both). */
  enabled: boolean;
  /** The last probe verdict (null until first tested). */
  lastStatus: EndpointStatus | null;
  lastErrorCategory: EndpointErrorCategory | null;
  /** A plain-language sentence the backend authored — rendered verbatim. */
  lastErrorDetail: string | null;
  /** ISO-8601 timestamp of the last probe (null until first tested). */
  lastCheckedAt: string | null;
}

/** The result of probing one endpoint (also persisted server-side). */
export interface EndpointTestResult {
  status: "ok" | "error";
  errorCategory: EndpointErrorCategory;
  errorDetail: string;
  checkedAt: string;
}

/** One model served by one endpoint, for the picker. */
export interface ModelChoice {
  model: string;
  endpointId: string;
}

/** A provider grouping for the picker: an endpoint and the models it serves. */
export interface ModelGroup {
  endpointId: string;
  endpointName: string;
  choices: ModelChoice[];
}

/** Whether an endpoint's models came from a live API, a configured default, or
 *  nothing at all — surfaced as a status badge in Settings. */
export type DiscoveryStatus = "live" | "default-only" | "unavailable";

export interface EndpointDiscovery {
  endpointId: string;
  endpointName: string;
  status: DiscoveryStatus;
  /** Count the provider's models API advertised (0 when unsupported/empty). */
  discovered: number;
  /** Whether the provider has a working models API. */
  supported: boolean;
}

/* ── Composite encoding (picker boundary only) ─────────────────────────────── */

const SEP = "::";

function encodeChoice(endpointId: string, model: string): string {
  return `${endpointId}${SEP}${model}`;
}

function decodeValue(value: string): ModelSelection | null {
  const i = value.indexOf(SEP);
  if (i < 0) return null;
  const endpointId = value.slice(0, i);
  const model = value.slice(i + SEP.length);
  return endpointId && model ? { endpointId, model } : null;
}

/* ── Role bindings (the backend source of truth) ───────────────────────────── */

/** A role binding: the ordered endpoint chain (a fallback chain, first = primary)
 *  plus an optional pinned model (`null` ⇒ the endpoint's own default). */
export interface RoleBinding {
  endpointIds: string[];
  model: string | null;
  /** The chain head's effective context window — the operator's override on the
   *  endpoint when set, else what the provider reports for the pinned model. Null when
   *  the role is unconfigured or neither could supply one.
   *
   *  On the binding rather than the endpoint because that is where the *model* is
   *  decided, and a window belongs to the (endpoint, model) pair. An endpoint row
   *  usually carries no default model at all. */
  contextWindow: number | null;
  /** True when this is the backend's default rather than the operator's choice — see
   *  `RoleViewDTO.implicit`. The picker shows it exactly as it shows a pinned model,
   *  because it is what a turn would actually run on; only surfaces that describe the
   *  *configuration* need to distinguish the two. */
  implicit: boolean;
}

/** role → its binding. */
export type RoleBindings = Record<string, RoleBinding>;

async function fetchRoles(): Promise<RoleBindings> {
  const dto = await api.get<Record<string, RoleViewDTO>>("/models/roles");
  const out: RoleBindings = {};
  for (const [role, v] of Object.entries(dto)) {
    out[role] = {
      endpointIds: v.endpoint_ids,
      model: v.model,
      contextWindow: v.context_window,
      implicit: v.implicit,
    };
  }
  return out;
}

/** The chat model = the backend `main` role binding's head endpoint + pinned
 *  model. `main` is single-endpoint (the picker overwrites the whole binding), so
 *  the head is the only endpoint.
 *
 *  This covers the *implicit* binding too: with nothing bound, the backend resolves
 *  `main` to the first usable endpoint/model and reports it here, flagged. That is
 *  deliberately not distinguished at this level — what a turn runs on is one fact,
 *  however it was arrived at, and the picker's job is to show that fact. */
function mainSelectionOf(
  roles: RoleBindings | undefined,
): ModelSelection | null {
  const main = roles?.main;
  const endpointId = main?.endpointIds?.[0];
  return endpointId && main?.model ? { endpointId, model: main.model } : null;
}

/* ── Backend DTO mappers ───────────────────────────────────────────────────── */

/** The single snake_case→camel mapper for an endpoint row, shared by the picker
 *  and Settings so a backend rename is fixed in one place. */
function toEndpoint(dto: EndpointViewDTO): ModelEndpoint {
  return {
    id: dto.id,
    name: dto.name,
    provider: dto.provider,
    baseUrl: dto.base_url,
    model: dto.model,
    hasApiKey: dto.has_api_key,
    contextWindow: dto.context_window,
    maxConcurrentRuns: dto.max_concurrent_runs,
    nativeTools: dto.native_tools,
    vision: dto.vision,
    thinking: dto.thinking,
    enabled: dto.enabled,
    lastStatus: dto.last_status,
    lastErrorCategory: dto.last_error_category,
    lastErrorDetail: dto.last_error_detail,
    lastCheckedAt: dto.last_checked_at,
  };
}

interface EndpointResult {
  endpointId: string;
  endpointName: string;
  supported: boolean;
  discovered: number;
  choices: ModelChoice[];
}

// A slow/unreachable provider must not freeze the picker on the backend's
// longer budget — bound each discovery call independently of the server.
const DISCOVERY_TIMEOUT_MS = 3000;

async function fetchEndpoints(): Promise<ModelEndpoint[]> {
  const rows = await api.get<EndpointViewDTO[]>("/models/endpoints");
  return rows.map(toEndpoint);
}

/* ── Provider presets (`GET /models/providers`) ────────────────────────────── */

/** One provider adapter's preset — what the endpoint editor / guided setup
 *  prefills from. Served by the backend; the frontend hardcodes no lab details. */
export interface ModelProvider {
  id: string;
  displayName: string;
  requiresKey: boolean;
  defaultBaseUrl: string | null;
  keyHint: string | null;
  docsUrl: string | null;
  nativeTools: boolean;
  vision: boolean;
}

function toProvider(dto: ProviderViewDTO): ModelProvider {
  return {
    id: dto.id,
    displayName: dto.display_name,
    requiresKey: dto.requires_key,
    defaultBaseUrl: dto.default_base_url,
    keyHint: dto.key_hint,
    docsUrl: dto.docs_url,
    nativeTools: dto.native_tools,
    vision: dto.vision,
  };
}

async function fetchProviders(): Promise<ModelProvider[]> {
  const rows = await api.get<ProviderViewDTO[]>("/models/providers");
  return rows.map(toProvider);
}

/** Discover the models one endpoint's provider serves (`GET …/{id}/models`).
 *  The single owner of that call — discovery and the guided connect flow both go
 *  through it. `supported` distinguishes a working-but-empty models API from one
 *  that couldn't be reached; on any failure it reads as unsupported + empty. */
export async function fetchEndpointModels(
  id: string,
): Promise<EndpointModelsDTO> {
  try {
    return await api.get<EndpointModelsDTO>(`/models/endpoints/${id}/models`, {
      signal: AbortSignal.timeout(DISCOVERY_TIMEOUT_MS),
    });
  } catch {
    return { models: [], supported: false };
  }
}

async function fetchDiscovery(
  endpoints: ModelEndpoint[],
): Promise<EndpointResult[]> {
  return Promise.all(
    endpoints.map(async (e): Promise<EndpointResult> => {
      const res = await fetchEndpointModels(e.id);
      let models = res.models;
      const supported = res.supported;
      const discovered = models.length;
      // Always keep the configured default selectable, leading the list.
      if (e.model && !models.includes(e.model)) models = [e.model, ...models];
      return {
        endpointId: e.id,
        endpointName: e.name,
        supported,
        discovered,
        choices: models.map((m) => ({ model: m, endpointId: e.id })),
      };
    }),
  );
}

function statusOf(r: EndpointResult): DiscoveryStatus {
  if (r.discovered > 0) return "live";
  return r.choices.length > 0 ? "default-only" : "unavailable";
}

/* ── The reactive store ─────────────────────────────────────────────────────────
   Owned by one app-lifetime root so the derivations can be memoized (computed
   once per change, shared across every surface) without dangling computations.
   The endpoints resource is the single `/models/endpoints` fetch; discovery is
   derived from it, so the catalog renders immediately and badges fill in. */

const store = createRoot(() => {
  const session = useSession();

  // The role bindings — one shared `/models/roles` fetch for the picker (which
  // derives the `main` selection from it), Settings' ROLE BINDINGS panel, and the
  // embedding controls. Every role write (`setRoleBinding`) ticks it, so all
  // three surfaces reconcile from the same re-read.
  const [rolesTick, setRolesTick] = createSignal(1);
  const [roles] = createResource(
    () => (session.isAuthenticated ? rolesTick() : false),
    fetchRoles,
  );

  // The chat model is the backend `main` role binding — the single source of
  // truth. `selection` is the local echo: seeded/reconciled from the binding and
  // moved optimistically by a pick (which writes the binding back).
  const [selection, setSelection] = createSignal<ModelSelection | null>(null);
  // Reconcile the echo with the backend binding whenever it (re)loads — on first
  // unlock, after our own optimistic write, and after a Settings edit refreshes it.
  // Only on `ready` (not mid-refetch), so an in-flight optimistic pick isn't clobbered.
  createComputed(() => {
    if (roles.state === "ready") setSelection(mainSelectionOf(roles.latest));
  });

  // The provider presets — a static catalog; fetched once per unlock.
  const [providers] = createResource(
    () => session.isAuthenticated,
    fetchProviders,
  );

  // The endpoint catalog — gated on unlock (a pre-auth call would 401); the tick
  // lets a write (create/update/delete) force a re-read, which cascades to
  // discovery since discovery's source is the endpoints list.
  const [endpointsTick, setEndpointsTick] = createSignal(1);
  const [endpoints] = createResource(
    () => (session.isAuthenticated ? endpointsTick() : false),
    fetchEndpoints,
  );

  // Disabled endpoints are excluded at the picker's source: discovery only runs
  // over live endpoints, so `groups`/`pickerGroups` never offer one and
  // `effective()` auto-falls to the next live choice. (Settings reads the full
  // catalog directly off `endpoints` to still show disabled rows.)
  // Discovery's source is the *discovery-relevant* projection of the live
  // endpoints — id + model + baseUrl + whether a key is set. A health-only
  // refresh (last_status/last_error_*/last_checked_at change after a probe or a
  // toggle on another endpoint) must NOT re-trigger the per-endpoint /models
  // fetch for every endpoint, so the memo keeps its identity when only those
  // fields moved. Membership still changes when `enabled` flips (the picker must
  // update), so that re-fires discovery as intended.
  const liveEndpoints = createMemo<ModelEndpoint[] | false>(
    () => {
      const all = settled(endpoints);
      return all ? all.filter((e) => e.enabled) : false;
    },
    false,
    {
      equals: (prev, next) => {
        if (prev === false || next === false) return prev === next;
        if (prev.length !== next.length) return false;
        return prev.every((a, i) => {
          const b = next[i];
          return (
            a.id === b.id &&
            a.model === b.model &&
            a.baseUrl === b.baseUrl &&
            a.hasApiKey === b.hasApiKey
          );
        });
      },
    },
  );
  // Discovery re-runs when the endpoints that feed it change — and when something
  // explicitly asks it to.
  //
  // The tick is not redundant with `endpointsTick`. The memo above deliberately keeps
  // its identity through a catalog re-read that didn't move any discovery-relevant
  // field, which is what stops a health probe re-fetching every endpoint's model list.
  // But *the set of models an endpoint serves is not one of those fields* — a local
  // engine that just started, or a provider that added a model, changes nothing in the
  // endpoint row, so re-reading the catalog can never surface it. Only asking the
  // endpoint again can, and this is how a caller asks.
  const [discoveryTick, setDiscoveryTick] = createSignal(1);
  const discoverySource = createMemo(() => {
    if (!session.isAuthenticated) return false;
    const live = liveEndpoints();
    // A fresh tuple per change is the point: its identity is what the resource keys
    // on, so it refetches on a tick even when the endpoint list is untouched.
    return live === false ? false : ([discoveryTick(), live] as const);
  });
  const [discovery] = createResource(discoverySource, ([, live]) =>
    fetchDiscovery(live),
  );

  const results = createMemo<EndpointResult[]>(() => settled(discovery) ?? []);
  const groups = createMemo<ModelGroup[]>(() =>
    results()
      .filter((r) => r.choices.length > 0)
      .map((r) => ({
        endpointId: r.endpointId,
        endpointName: r.endpointName,
        choices: r.choices,
      })),
  );
  // The picker's options — every discovered model, plus the current selection when
  // discovery doesn't list it.
  //
  // That last part is not a nicety. The Combobox labels its trigger by finding the
  // value among its options, so a selection absent from the list renders as the
  // placeholder — "No model" — while the backend would happily run it. Discovery
  // failing (or simply lagging a model served a moment ago) would then blank the
  // picker on a perfectly working thread, which is the same class of lie as the
  // display fallback this replaced, only pointing the other way.
  const pickerGroups = createMemo(() => {
    const out = groups().map((g) => ({
      label: g.endpointName,
      options: g.choices.map((c) => ({
        value: encodeChoice(c.endpointId, c.model),
        label: c.model,
      })),
    }));
    const sel = selection();
    if (!sel) return out;
    const value = encodeChoice(sel.endpointId, sel.model);
    if (out.some((g) => g.options.some((o) => o.value === value))) return out;
    const endpoint = (settled(endpoints) ?? []).find(
      (e) => e.id === sel.endpointId,
    );
    const group = out.find((g) => g.label === endpoint?.name);
    if (group) group.options = [...group.options, { value, label: sel.model }];
    else
      out.push({
        label: endpoint?.name ?? "Selected",
        options: [{ value, label: sel.model }],
      });
    return out;
  });
  const discoveries = createMemo<EndpointDiscovery[]>(() =>
    results().map((r) => ({
      endpointId: r.endpointId,
      endpointName: r.endpointName,
      discovered: r.discovered,
      supported: r.supported,
      status: statusOf(r),
    })),
  );
  // What the picker shows, and what a turn will run on — the same answer, because
  // both come from the backend's `main` binding.
  //
  // This used to fall back to "the first discovered choice" when the binding did not
  // match one, which made the composer display a model that nothing had selected: the
  // send resolves `main` and sends no override, so SEND then refused the very model
  // shown above it, and re-picking that same model in the dropdown was the only way to
  // reconcile them. The backend now reports an implicit `main` for exactly the case
  // that fallback was papering over, so the guess is gone.
  //
  // Discovery is deliberately not consulted. It can be unreachable or lag a newly
  // served model, and in that state the honest answer is still the model the backend
  // would resolve — not some other model that happens to be listed. `pickerGroups`
  // below carries the selection as an option when discovery lacks it, so the trigger
  // can always label what will run.
  const effective = createMemo<ModelSelection | null>(
    () => selection() ?? null,
  );

  // The endpoint backing the effective pick — the single source for its metadata
  // (provider name, context window) so consumers don't re-derive the lookup.
  const effectiveEndpoint = createMemo<ModelEndpoint | null>(() => {
    const sel = effective();
    if (!sel) return null;
    return (
      (settled(endpoints) ?? []).find((e) => e.id === sel.endpointId) ?? null
    );
  });

  return {
    selection,
    setSelection,
    roles,
    refreshRoles: () => setRolesTick((t) => t + 1),
    providers,
    endpoints,
    setEndpointsTick,
    setDiscoveryTick,
    discovery,
    groups,
    pickerGroups,
    discoveries,
    effective,
    effectiveEndpoint,
  };
});

/* ── Public surface ─────────────────────────────────────────────────────────── */

/** The operator's explicit pick (or null before they've chosen). Prefer
 *  `effectiveValue()` for display and `effectiveSelection()` for sending. */
export const selectedModel = store.selection;

/** The single `PUT /models/roles/{role}` — every role write in the app funnels
 *  through here (no seam issues its own fetch). */
async function putRoleBinding(
  role: string,
  endpointIds: string[],
  model: string | null,
): Promise<void> {
  await api.put(`/models/roles/${role}`, {
    endpoint_ids: endpointIds,
    model,
  });
}

/** The role bindings resource — shared by Settings' ROLE BINDINGS panel and the
 *  embedding controls (the picker derives the `main` selection from it). */
export function useRoles(): Resource<RoleBindings> {
  return store.roles;
}

/** Re-read the role bindings (cascades to the top-bar picker's `main` echo). */
export function refreshRoles(): void {
  store.refreshRoles();
}

/** Bind a role to an ordered endpoint chain (plus an optional pinned model), then
 *  re-read the shared bindings so every surface — including the top-bar picker
 *  when `main` moved — reconciles. Errors are intentionally *not* swallowed: the
 *  backend rejects an invalid bind with a 422 whose plain-language detail the
 *  caller surfaces verbatim. */
export async function setRoleBinding(
  role: string,
  endpointIds: string[],
  model: string | null = null,
): Promise<void> {
  try {
    await putRoleBinding(role, endpointIds, model);
  } finally {
    store.refreshRoles();
  }
}

/** Whether two picks name the same model on the same endpoint. Compared by value,
 *  not identity: the echo is re-seated from the role bindings on every reconcile, so
 *  the object holding a selection is routinely replaced by an equal one. */
function sameSelection(
  a: ModelSelection | null,
  b: ModelSelection | null,
): boolean {
  if (!a || !b) return a === b;
  return a.endpointId === b.endpointId && a.model === b.model;
}

/** Persist the chat model by writing the backend `main` binding (single endpoint +
 *  pinned model), moving the local echo optimistically and reconciling from the
 *  backend after — rolling the echo back if the write fails. `main` is
 *  single-endpoint, so this overwrites the whole binding. A null pick is a no-op
 *  write (the picker never clears the binding). */
export async function setSelectedModel(
  sel: ModelSelection | null,
): Promise<void> {
  if (!sel) return;
  const previous = store.selection();
  store.setSelection(sel); // optimistic
  try {
    await putRoleBinding("main", [sel.endpointId], sel.model);
    store.refreshRoles(); // reconcile with the persisted binding
  } catch (e) {
    // Roll back only while the echo is still showing *this* call's pick. A slow
    // write that fails is not evidence about whatever the operator picked after it:
    // restoring `previous` unconditionally would undo a later pick that the backend
    // accepted, putting the picker on a model nobody chose and nothing persisted —
    // the one wrong answer worse than leaving the failed one on screen. When a newer
    // pick owns the echo, the `refreshRoles` below is what confirms it.
    if (sameSelection(store.selection(), sel)) store.setSelection(previous);
    store.refreshRoles();
    throw e;
  }
}

/** Combobox onChange adapter: decode the option value into a structured pick.
 *  Fire-and-forget — the optimistic echo (and its rollback on failure) is the UX. */
export function selectModelByValue(value: string): void {
  void setSelectedModel(decodeValue(value)).catch((e) =>
    console.error("failed to persist model selection", e),
  );
}

/** Encode a structured selection into the composite string the picker uses as a
 *  value. For surfaces that own a *local* model pick (e.g. each compare pane)
 *  rather than the global sticky selection. */
export function encodeModelValue(sel: ModelSelection): string {
  return encodeChoice(sel.endpointId, sel.model);
}

/** Decode a picker composite value back to a structured selection (null if
 *  malformed) — the read side of `encodeModelValue` for locally-owned picks. */
export function decodeModelValue(value: string): ModelSelection | null {
  return decodeValue(value);
}

/** The endpoint catalog resource — shared by the picker and Settings. */
export function useEndpoints(): Resource<ModelEndpoint[]> {
  return store.endpoints;
}

/** The provider presets (`GET /models/providers`) — what the endpoint editor and
 *  the guided setup prefill from. */
export function useProviders(): Resource<ModelProvider[]> {
  return store.providers;
}

/** Re-read the catalog (after a create/update/delete); cascades to discovery. */
export function refreshEndpoints(): void {
  store.setEndpointsTick((t) => t + 1);
}

/** Re-ask every live endpoint what models it serves.
 *
 *  Distinct from `refreshEndpoints`, which re-reads our own catalog: this is the only
 *  call that can surface a model that already existed on the provider's side — a local
 *  engine started since the app loaded, or a model added upstream. Nothing about the
 *  stored endpoint row changes in either case, so no catalog re-read would notice.
 *
 *  Wired to the moment a model dropdown *opens*, rather than to a refresh button beside
 *  it. Opening the list is already the operator saying "show me what I can pick" — the
 *  button was asking them to say it twice, and to know that the first list might be
 *  stale. It refetches in the background: the current options stay on screen and are
 *  replaced when the answer lands, so the menu never opens empty or jumps under the
 *  cursor. */
export function refreshModels(): void {
  store.setDiscoveryTick((t) => t + 1);
}

/** Whether a discovery refetch is in flight — for a quiet inline note in a dropdown
 *  that is showing the previous answer while it waits. */
export function modelsRefreshing(): boolean {
  return store.discovery.loading;
}

/** Probe an endpoint now. The backend persists the verdict, so we re-read the
 *  catalog afterwards to reflect the new `last_*` fields; the verdict is also
 *  returned so the caller can surface it immediately. */
export async function testEndpoint(id: string): Promise<EndpointTestResult> {
  const dto = await api.post<EndpointTestDTO>(
    `/models/endpoints/${id}/test`,
    {},
  );
  refreshEndpoints();
  return {
    status: dto.status,
    errorCategory: dto.error_category,
    errorDetail: dto.error_detail,
    checkedAt: dto.checked_at,
  };
}

/** Enable/disable an endpoint, then re-read the catalog (cascades to the picker,
 *  which excludes disabled endpoints at its source). */
export async function setEndpointEnabled(
  id: string,
  enabled: boolean,
): Promise<void> {
  await api.patch(`/models/endpoints/${id}`, { enabled });
  refreshEndpoints();
}

/** Endpoints with at least one selectable model, grouped for the picker. */
export function modelGroups(): ModelGroup[] {
  return store.groups();
}

/** The discovered models shaped for a grouped picker (`~/ui` Combobox). */
export function modelPickerGroups(): {
  label: string;
  options: { value: string; label: string }[];
}[] {
  return store.pickerGroups();
}

/** Per-endpoint discovery state for Settings (badges + failure surfacing). */
export function endpointDiscovery(): EndpointDiscovery[] {
  return store.discoveries();
}

export function effectiveSelection(): ModelSelection | null {
  return store.effective();
}

/** The composite value the picker should show as active (effective pick). */
export function effectiveValue(): string {
  const sel = store.effective();
  return sel ? encodeChoice(sel.endpointId, sel.model) : "";
}

/** The model id to display (or "" when nothing is configured). */
export function selectedModelLabel(): string {
  return store.effective()?.model ?? "";
}

/** The context window the active pick will actually run under — the operator's
 *  override when set, else what the provider reported. Null when neither could supply
 *  one, or when nothing is configured at all.
 *
 *  Read off the `main` **binding**, not off its endpoint. A window belongs to the
 *  (endpoint, model) pair, and the model is the binding's: an endpoint row usually
 *  carries no default model at all, so reading its column answered "no window" against
 *  a server reporting 262144 perfectly well — which left the dashboard's context figure
 *  blank and, once the gate landed, had the composer refusing to send.
 *
 *  **`.latest`, not the plain resource read, and that is load-bearing.** A bare
 *  `roles()` registers with the nearest Suspense boundary on *every* load — a
 *  refetch included — and picking a model refetches the bindings by design. The
 *  chat screen renders inside the shell's content Suspense and reads this through
 *  the composer's send gate, so each pick suspended the whole content region for
 *  the length of one round-trip; Suspense detaches its children while suspended,
 *  which silently reset the transcript's scroll to the top. `.latest` is the same
 *  reactive value without the suspense registration, so the gate re-reads the new
 *  window when it lands and nothing unmounts in between. */
export function effectiveContextWindow(): number | null {
  return settled(store.roles)?.main?.contextWindow ?? null;
}

/** `sendBlocker` applied to the live selection — the accessor a composer binds to. The
 *  rule itself, and why it stays quiet on an empty workspace, lives in `sendGate.ts`. */
export function sendBlockedReason(): string | null {
  return sendBlocker(store.effective() !== null, effectiveContextWindow());
}
