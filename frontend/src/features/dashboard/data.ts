import {
  createEffect,
  createResource,
  on,
  onCleanup,
  type Resource,
} from "solid-js";
import { api } from "~/lib/api";
import { num } from "~/lib/format";
import { useNotifications } from "~/lib/stores/notifications";
import type {
  ActiveRun,
  CapabilityHealth,
  Overview,
  SystemStat,
} from "./model";

/* ── Backend DTOs ──────────────────────────────────────────────────────────── */

interface CapabilityDTO {
  key: string;
  label: string;
  status: CapabilityHealth["status"];
  detail: string;
  critical: boolean;
  remediation_href: string | null;
  remediation_label: string | null;
}

interface OverviewDTO {
  version: string;
  endpoint_count: number;
  conversation_count: number;
  memory_count: number;
  capabilities: CapabilityDTO[];
}

interface RunDTO {
  id: string;
  kind: string;
  status: string;
  // Snake_case on the wire because, unlike the two below, these carry no
  // serialization alias on `RunView` (`backend/routes/runs.py`). Worth knowing
  // before assuming one convention across this DTO.
  created_at: string;
  started_at?: string | null;
  ended_at?: string | null;
  conversationId?: string | null;
  conversationTitle?: string | null;
}

/* ── Mappers (snake_case DTO → seam type) ──────────────────────────────────── */

function toCapability(dto: CapabilityDTO): CapabilityHealth {
  return {
    key: dto.key,
    label: dto.label,
    status: dto.status,
    detail: dto.detail,
    critical: dto.critical,
    remediationHref: dto.remediation_href ?? undefined,
    remediationLabel: dto.remediation_label ?? undefined,
  };
}

function toOverview(dto: OverviewDTO): Overview {
  return {
    version: dto.version,
    endpointCount: dto.endpoint_count,
    conversationCount: dto.conversation_count,
    memoryCount: dto.memory_count,
    capabilities: dto.capabilities.map(toCapability),
  };
}

/** Human readout for an active run's status (the IN FLIGHT detail column). */
const RUN_STATUS_LABEL: Record<string, string> = {
  running: "RUNNING",
  queued: "QUEUED",
  awaiting_input: "NEEDS APPROVAL",
};

function toActiveRun(dto: RunDTO): ActiveRun {
  const status = (dto.status as ActiveRun["status"]) ?? "running";
  return {
    id: dto.id,
    kind: dto.kind.toUpperCase(),
    // A run carries no human title of its own; its linked conversation's title
    // (when present) is far more useful than the bare kind, which is kept as the
    // fallback for a run with no conversation (or one the backend hasn't titled yet).
    label: dto.conversationTitle ?? `${dto.kind} run`,
    status,
    detail: RUN_STATUS_LABEL[dto.status] ?? dto.status.toUpperCase(),
    // A queued run has been created but not started; its clock reads `--:--:--`
    // until it does, which is the honest reading. Falling back to `created_at`
    // here would show queue time as though it were run time.
    startedAt: dto.started_at ?? undefined,
    endedAt: dto.ended_at ?? undefined,
    conversationId: dto.conversationId ?? undefined,
  };
}

/* ── The system strip's facts band (presentation shaping of real overview data) ─ */

/** The telemetry strip as labelled cells, in glance order. Only facts the
 *  backend actually reports — no fabricated tok/s, VRAM, or uplink. The active
 *  model + its context window are the top-bar picker's live selection (passed
 *  in), not a backend-bound default. */
export function overviewBand(
  o: Overview,
  activeModel: string,
  contextWindow: number | null,
): SystemStat[] {
  const band: SystemStat[] = [{ label: "MODEL", value: activeModel || "—" }];
  if (contextWindow != null)
    band.push({ label: "CTX", value: num(contextWindow, 0) });
  band.push(
    { label: "THREADS", value: num(o.conversationCount, 0) },
    { label: "MEMORIES", value: num(o.memoryCount, 0) },
    { label: "ENDPOINTS", value: num(o.endpointCount, 0) },
    { label: "VERSION", value: o.version },
  );
  return band;
}

/* ── Read accessors (the seam) ─────────────────────────────────────────────── */

async function fetchOverview(): Promise<Overview> {
  return toOverview(await api.get<OverviewDTO>("/overview"));
}

export interface UseOverviewResult {
  data: Resource<Overview>;
  refetch: () => void;
}

export function useOverview(): UseOverviewResult {
  const [data, { refetch }] = createResource(fetchOverview);
  return { data, refetch };
}

async function fetchActiveRuns(): Promise<ActiveRun[]> {
  // The backend already defaults to active-only; pass it explicitly so this
  // call reads as scoped rather than relying on an implicit server default.
  const rows = await api.get<RunDTO[]>("/runs?active=true");
  return rows.map(toActiveRun);
}

export interface UseActiveRunsResult {
  data: Resource<ActiveRun[]>;
  refetch: () => void;
}

/** How often to re-poll the IN FLIGHT panel while it's mounted. Active runs are
 *  short-lived by nature (queued → running → terminal, or parked awaiting a
 *  decision), so a modest interval keeps the panel honest without a second SSE
 *  subscription — the run substrate's own stream is per-run, not a list feed. */
const POLL_INTERVAL_MS = 15_000;

/** The dashboard's active-runs list, kept fresh three ways: a poll while mounted,
 *  a refetch when the tab regains focus (a run may have started/finished/parked
 *  while the operator was elsewhere), and a refetch nudged by the notification
 *  store — an `approval_needed`/`run_completed`/`run_failed` notification is
 *  exactly the signal that some active run just changed, so this subscribes to
 *  that store rather than opening a second stream. */
export function useActiveRuns(): UseActiveRunsResult {
  const [data, { refetch }] = createResource(fetchActiveRuns);

  const onFocus = () => refetch();
  window.addEventListener("focus", onFocus);
  onCleanup(() => window.removeEventListener("focus", onFocus));

  const interval = window.setInterval(() => {
    if (document.visibilityState === "visible") void refetch();
  }, POLL_INTERVAL_MS);
  onCleanup(() => window.clearInterval(interval));

  // `items` is a fresh array on every created/updated notification (the store's
  // upsert always replaces it — see `~/lib/stores/notifications`), so tracking its
  // reference is enough to catch both kinds without inspecting event types.
  // `defer: true` skips the run this effect's own subscription triggers on mount.
  const notifications = useNotifications();
  createEffect(
    on(
      () => notifications.items,
      () => refetch(),
      { defer: true },
    ),
  );

  return { data, refetch };
}
