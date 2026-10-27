/** The launchpad's activity ledger and its one-line digest: pure derivations over the
 *  thread list, the live runs and capability health. Nothing here fetches or ticks. */

import type { ChatSummary } from "~/features/chat/model";
import { resolveRunState, type RunState } from "~/features/chat/runState";
import { parseInstant } from "~/lib/format";
import type { ActionTone } from "~/ui";
import type { ActiveRun, CapabilityHealth } from "./model";

export interface ActivityItem {
  key: string;
  /** The thread to open; absent for a stateless agent run, which has none. */
  conversationId?: string;
  title: string;
  agentName?: string;
  workspace?: string;
  model?: string;
  state?: RunState;
  /** What a run waiting on the operator is waiting for. */
  awaitingKind?: ActionTone;
  /** The live run's start, for its clock. */
  startedAt?: string;
  /** What the live run is doing or waiting on, in the backend's words. */
  detail?: string;
  updatedAt?: string;
}

export interface ActivityCounts {
  /** Live runs not waiting on the operator: running or queued. */
  running: number;
  waiting: number;
  /** Threads whose last run failed within `FAILURE_WINDOW_MS`. */
  failed: number;
}

export interface Activity {
  items: ActivityItem[];
  /** Every thread and stateless run, before the limit. */
  total: number;
  /** Over every row, not only the shown ones, so the digest agrees with the ledger. */
  counts: ActivityCounts;
}

/** A failure stops being news after a day: `lastOutcome` outlives the operator
 *  having read it, and there is no read state to clear it with. */
export const FAILURE_WINDOW_MS = 24 * 60 * 60 * 1000;

const LIVE_RANK: Record<string, number> = {
  awaiting_input: 0,
  running: 1,
  queued: 2,
};

function liveRank(state: RunState | undefined): number {
  return state !== undefined && state in LIVE_RANK
    ? LIVE_RANK[state]!
    : Number.POSITIVE_INFINITY;
}

function instant(iso: string | undefined): number {
  const t = iso ? parseInstant(iso) : NaN;
  return Number.isNaN(t) ? 0 : t;
}

/** Live work first (awaiting you, then running, then queued), then threads by their
 *  last update. A stateless agent run has no thread, so it is its own row. A run whose
 *  thread is not in `sessions` (a sub-agent's hidden one) is neither shown nor counted. */
export function buildActivity(
  sessions: readonly ChatSummary[],
  runs: readonly ActiveRun[],
  limit: number,
  now: number,
): Activity {
  const runFor = new Map(
    runs
      .filter((r) => r.conversationId)
      .map((r) => [r.conversationId!, r] as const),
  );
  const threads: ActivityItem[] = sessions.map((s) => {
    const run = s.activity ? runFor.get(s.id) : undefined;
    return {
      key: s.id,
      conversationId: s.id,
      title: s.title,
      agentName: s.agentName,
      workspace: s.workspace,
      model: s.model,
      state: resolveRunState(s.activity, s.lastOutcome),
      awaitingKind: s.awaitingKind ?? run?.awaitingKind,
      startedAt: run?.startedAt,
      detail: run?.detail || undefined,
      updatedAt: s.updatedAt,
    };
  });
  const stateless: ActivityItem[] = runs
    .filter((r) => !r.conversationId)
    .map((r) => ({
      key: `run:${r.id}`,
      title: r.label,
      state: r.status,
      awaitingKind: r.awaitingKind,
      startedAt: r.startedAt,
      detail: r.detail || undefined,
    }));
  const all = [...threads, ...stateless].sort(
    (a, b) =>
      liveRank(a.state) - liveRank(b.state) ||
      instant(b.updatedAt ?? b.startedAt) - instant(a.updatedAt ?? a.startedAt),
  );
  const counts: ActivityCounts = {
    running: all.filter((i) => i.state === "running" || i.state === "queued")
      .length,
    waiting: all.filter((i) => i.state === "awaiting_input").length,
    failed: all.filter(
      (i) =>
        i.state === "error" && now - instant(i.updatedAt) < FAILURE_WINDOW_MS,
    ).length,
  };
  return { items: all.slice(0, limit), total: all.length, counts };
}

/** Degraded capabilities, alerts before warnings. */
export function degraded(
  caps: readonly CapabilityHealth[],
): CapabilityHealth[] {
  return [
    ...caps.filter((c) => c.status === "alert"),
    ...caps.filter((c) => c.status === "warn"),
  ];
}

/** Capabilities the operator has not set up or has switched off. */
export function unconfigured(
  caps: readonly CapabilityHealth[],
): CapabilityHealth[] {
  return caps.filter((c) => c.status === "off");
}

const WORDS = [
  "zero",
  "one",
  "two",
  "three",
  "four",
  "five",
  "six",
  "seven",
  "eight",
  "nine",
];

function count(n: number): string {
  const word = WORDS[n] ?? String(n);
  return word.charAt(0).toUpperCase() + word.slice(1);
}

export interface DigestInput extends ActivityCounts {
  /** Degraded capabilities the backend marks critical. */
  critical: readonly Pick<CapabilityHealth, "label" | "status">[];
}

/** One plain sentence or three about the state of the workspace, in the interface's
 *  voice: what is running, what needs the operator, what broke. */
export function homeDigest(input: DigestInput): string {
  const { running, waiting, failed, critical } = input;
  const parts: string[] = [];
  if (running === 0 && waiting === 0) parts.push("Nothing is running.");
  if (running > 0) {
    parts.push(`${count(running)} ${running === 1 ? "run" : "runs"} going.`);
  }
  if (waiting > 0) {
    const subject =
      running > 0
        ? count(waiting)
        : `${count(waiting)} ${waiting === 1 ? "thread" : "threads"}`;
    parts.push(`${subject} ${waiting === 1 ? "is" : "are"} waiting on you.`);
  }
  if (failed > 0) {
    parts.push(
      `${count(failed)} ${failed === 1 ? "thread" : "threads"} ended in an error in the last day.`,
    );
  }
  for (const cap of critical) {
    parts.push(
      `${cap.label} is ${cap.status === "alert" ? "unavailable" : "degraded"}.`,
    );
  }
  return parts.join(" ");
}
