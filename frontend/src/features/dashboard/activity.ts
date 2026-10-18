/** The launchpad's activity ledger and its one-line digest: pure derivations over the
 *  thread list, the live runs and capability health. Nothing here fetches or ticks. */

import type { ChatSummary } from "~/features/chat/model";
import { resolveRunState, type RunState } from "~/features/chat/runState";
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
  /** The live run's start, for its clock. */
  startedAt?: string;
  updatedAt?: string;
}

export interface Activity {
  items: ActivityItem[];
  /** Every thread and stateless run, before the limit. */
  total: number;
}

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

function newestFirst(a?: string, b?: string): number {
  return (b ?? "").localeCompare(a ?? "");
}

/** Live work first (awaiting you, then running, then queued), then threads by their
 *  last update. A stateless agent run has no thread, so it is its own row. */
export function buildActivity(
  sessions: readonly ChatSummary[],
  runs: readonly ActiveRun[],
  limit: number,
): Activity {
  const runFor = new Map(
    runs
      .filter((r) => r.conversationId)
      .map((r) => [r.conversationId!, r] as const),
  );
  const threads: ActivityItem[] = sessions.map((s) => ({
    key: s.id,
    conversationId: s.id,
    title: s.title,
    agentName: s.agentName,
    workspace: s.workspace,
    model: s.model,
    state: resolveRunState(s.activity, s.lastOutcome),
    startedAt: s.activity ? runFor.get(s.id)?.startedAt : undefined,
    updatedAt: s.updatedAt,
  }));
  const stateless: ActivityItem[] = runs
    .filter((r) => !r.conversationId)
    .map((r) => ({
      key: `run:${r.id}`,
      title: r.label,
      state: r.status,
      startedAt: r.startedAt,
    }));
  const all = [...threads, ...stateless].sort(
    (a, b) =>
      liveRank(a.state) - liveRank(b.state) ||
      newestFirst(a.updatedAt ?? a.startedAt, b.updatedAt ?? b.startedAt),
  );
  return { items: all.slice(0, limit), total: all.length };
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

export interface DigestInput {
  /** Live runs not waiting on the operator: running or queued. */
  running: number;
  waiting: number;
  failed: number;
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
      `${count(failed)} ${failed === 1 ? "thread" : "threads"} ended in an error.`,
    );
  }
  for (const cap of critical) {
    parts.push(
      `${cap.label} is ${cap.status === "alert" ? "unavailable" : "degraded"}.`,
    );
  }
  return parts.join(" ");
}
