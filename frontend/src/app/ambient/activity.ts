import type { PulseTone } from "~/ui";
import type {
  ChatActivity,
  ChatOutcome,
  ChatSummary,
} from "~/features/chat/model";

type Snapshot = ReadonlyMap<
  string,
  { activity?: ChatActivity; lastOutcome?: ChatOutcome }
>;

export interface ActivityPulse {
  threadId: string;
  tone: PulseTone;
}

const OUTCOME_TONE: Record<ChatOutcome, PulseTone> = {
  done: "nominal",
  error: "alert",
  blocked: "alert",
  cancelled: "neutral",
};

/** The field's load: one per live thread, two for one waiting on the operator. */
export function activityLoad(rows: readonly ChatSummary[]): number {
  let load = 0;
  for (const s of rows) {
    if (s.activity === "awaiting_input") load += 2;
    else if (s.activity) load += 1;
  }
  return load;
}

/** The transitions between two reads of the thread list that the field rings for.
 *  With no previous read there is nothing to compare, so the first list fires nothing. */
export function diffActivity(
  prev: Snapshot | undefined,
  rows: readonly ChatSummary[],
): { pulses: ActivityPulse[]; snapshot: Snapshot } {
  const snapshot = new Map(
    rows.map((s) => [
      s.id,
      { activity: s.activity, lastOutcome: s.lastOutcome },
    ]),
  );
  if (!prev) return { pulses: [], snapshot };
  const pulses: ActivityPulse[] = [];
  for (const s of rows) {
    const was = prev.get(s.id)?.activity;
    const now = s.activity;
    if (now === was) continue;
    if (now === "awaiting_input") pulses.push({ threadId: s.id, tone: "warn" });
    else if (now && !was) pulses.push({ threadId: s.id, tone: "info" });
    else if (!now && was)
      pulses.push({
        threadId: s.id,
        tone: s.lastOutcome ? OUTCOME_TONE[s.lastOutcome] : "neutral",
      });
  }
  return { pulses, snapshot };
}
