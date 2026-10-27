/** The dashboard (home) data contract. Every type here mirrors a real backend
 *  shape — the home page is a launchpad into chat plus a faithful readout of
 *  what the backend actually knows (`GET /overview`, `GET /runs`). It invents
 *  nothing: telemetry/services that don't exist yet simply aren't represented. */

import type { ActionTone } from "~/ui";

/** A single labelled telemetry cell in the system strip. */
export interface SystemStat {
  label: string;
  value: string;
}

/** A capability's backend-decided health. The backend owns the policy (what
 *  counts as nominal/degraded/down) and the remediation target; the frontend
 *  only renders it. Mirrors the overview `Capability`. */
export interface CapabilityHealth {
  /** Stable id: `chat_model` | `embeddings` | `sandbox`. */
  key: string;
  label: string;
  /** `off`: not set up or switched off by the operator, which is not a fault. */
  status: "nominal" | "warn" | "alert" | "off";
  detail: string;
  /** A capability the workspace cannot function without — drives overall status. */
  critical: boolean;
  /** Where to go to fix a degraded/down capability. */
  remediationHref?: string;
  remediationLabel?: string;
}

/** The home overview aggregate — the single source of truth the launchpad reads
 *  for its facts band and capability health. */
export interface Overview {
  version: string;
  endpointCount: number;
  conversationCount: number;
  memoryCount: number;
  capabilities: CapabilityHealth[];
}

/** A run still in flight (not yet terminal) — the IN FLIGHT panel's unit. */
export interface ActiveRun {
  id: string;
  /** Display tag: CHAT / AGENT. */
  kind: string;
  /** Human one-liner for the row. */
  label: string;
  status: "running" | "queued" | "awaiting_input";
  /** Compact status readout, e.g. RUNNING / QUEUED; empty while parked on the operator,
   *  where `awaitingKind` names the wait. */
  detail: string;
  awaitingKind?: ActionTone;
  /** When the run actually began, ISO-8601 — the run clock's input. Absent while
   *  the run is still queued, which is a different fact from "started just now". */
  startedAt?: string;
  /** When it finished, ISO-8601. Stops the clock: a run clock still counting after
   *  the run ended reports the age of a record as though it were work in progress. */
  endedAt?: string;
  /** The conversation this run belongs to, when it's chat-linked — the row is
   *  clickable straight through to it. Absent for a stateless run. */
  conversationId?: string;
}
