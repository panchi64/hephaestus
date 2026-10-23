import type { StrikeTone } from "~/ui";
import type {
  ChatActivity,
  ChatOutcome,
  ChatSummary,
} from "~/features/chat/model";

type Snapshot = ReadonlyMap<
  string,
  { activity?: ChatActivity; lastOutcome?: ChatOutcome }
>;

const OUTCOME_TONE: Record<ChatOutcome, StrikeTone> = {
  done: "nominal",
  error: "alert",
  blocked: "alert",
  cancelled: "neutral",
};

/** The field's load: one per live top-level thread, two for one waiting on the
 *  operator. Threads an agent spawned are the aurora's, not the load's. */
export function activityLoad(rows: readonly ChatSummary[]): number {
  let load = 0;
  for (const s of rows) {
    if (s.parentId) continue;
    if (s.activity === "awaiting_input") load += 2;
    else if (s.activity) load += 1;
  }
  return load;
}

/** Live threads an agent spawned: sub-agents, and threads opened from inside another. */
export function liveSubagents(rows: readonly ChatSummary[]): number {
  return rows.filter((s) => s.parentId && s.activity).length;
}

/** The transitions between two reads of the thread list that strike lightning. Spawned
 *  threads never strike; they show as aurora. With no previous read there is nothing
 *  to compare, so the first list fires nothing. */
export function diffActivity(
  prev: Snapshot | undefined,
  rows: readonly ChatSummary[],
): { strikes: StrikeTone[]; snapshot: Snapshot } {
  const snapshot = new Map(
    rows.map((s) => [
      s.id,
      { activity: s.activity, lastOutcome: s.lastOutcome },
    ]),
  );
  if (!prev) return { strikes: [], snapshot };
  const strikes: StrikeTone[] = [];
  for (const s of rows) {
    if (s.parentId) continue;
    const was = prev.get(s.id)?.activity;
    const now = s.activity;
    if (now === was) continue;
    if (now === "awaiting_input") strikes.push("warn");
    else if (now && !was) strikes.push("info");
    else if (!now && was)
      strikes.push(s.lastOutcome ? OUTCOME_TONE[s.lastOutcome] : "neutral");
  }
  return { strikes, snapshot };
}
