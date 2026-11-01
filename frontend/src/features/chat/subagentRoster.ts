import type { TextTone } from "~/ui";
import type { Subagent } from "./data";

/** Waiting on the operator first, then working, then whatever went wrong. Not
 *  chronological: ordering by start time puts the one blocked row wherever it falls. */
const RANK: Record<string, number> = {
  blocked: 0,
  running: 1,
  failed: 2,
  stopped: 3,
  cancelled: 4,
};

export function byUrgency(a: Subagent, b: Subagent): number {
  const rank = (RANK[a.status] ?? 9) - (RANK[b.status] ?? 9);
  return rank !== 0 ? rank : b.startedAt.localeCompare(a.startedAt);
}

export interface Placed {
  subagent: Subagent;
  level: number;
}

/** Depth-first, each card directly beneath its parent. A card whose parent is not in
 *  the list (the viewed thread, or one filtered into the other roster) is top level. */
export function arrange(cards: Subagent[]): Placed[] {
  const owned = new Set(cards.map((c) => c.conversationId));
  const children = new Map<string, Subagent[]>();
  const top: Subagent[] = [];
  for (const card of cards) {
    const parent = card.parentConversationId;
    if (owned.has(parent) && parent !== card.conversationId) {
      const siblings = children.get(parent);
      if (siblings) siblings.push(card);
      else children.set(parent, [card]);
    } else top.push(card);
  }
  const placed: Placed[] = [];
  const seen = new Set<string>();
  const walk = (card: Subagent, level: number) => {
    if (seen.has(card.id)) return;
    seen.add(card.id);
    placed.push({ subagent: card, level });
    for (const child of children.get(card.conversationId) ?? [])
      walk(child, level + 1);
  };
  for (const card of top) walk(card, 0);
  // A parent cycle leaves cards unreached from `top`; they still get a row.
  for (const card of cards) walk(card, 0);
  return placed;
}

/** How full its context window is, 0–100. Null when nothing has measured it yet:
 *  an empty gauge would claim the sub-agent has done nothing. */
export function fullness(
  subagent: Pick<Subagent, "contextUsed" | "contextWindow">,
): number | null {
  const { contextUsed: used, contextWindow: window } = subagent;
  if (used === null || window === null || window <= 0) return null;
  return Math.min(100, Math.round((used / window) * 100));
}

/** Fixed thresholds rather than the operator's composer ones: a roster row is a
 *  glance, not a setting. */
export function gaugeTone(
  value: number,
): Extract<TextTone, "default" | "warn" | "alert"> {
  if (value >= 90) return "alert";
  return value >= 75 ? "warn" : "default";
}

/** Its latest answer while it runs, its report once done, and why for a failure. */
export function latestLine(
  subagent: Pick<Subagent, "status" | "summary" | "error">,
): string {
  if (subagent.status === "failed") return subagent.error ?? "failed";
  if (subagent.summary) return subagent.summary;
  return subagent.status === "running" || subagent.status === "blocked"
    ? "working…"
    : "reported nothing";
}

export interface RosterCounts {
  waiting: number;
  working: number;
  failed: number;
  done: number;
}

export function rosterCounts(subagents: Subagent[]): RosterCounts {
  const counts: RosterCounts = { waiting: 0, working: 0, failed: 0, done: 0 };
  for (const s of subagents) {
    if (s.status === "blocked") counts.waiting++;
    else if (s.status === "running") counts.working++;
    else if (s.status === "failed") counts.failed++;
    else counts.done++;
  }
  return counts;
}

/** `1 waiting · 2 working · 1 failed`, zero terms dropped. With nothing live or
 *  failed it is the total that finished; with no sub-agents at all, empty. */
export function countLabel(counts: RosterCounts): string {
  const terms = [
    counts.waiting > 0 && `${counts.waiting} waiting`,
    counts.working > 0 && `${counts.working} working`,
    counts.failed > 0 && `${counts.failed} failed`,
  ].filter(Boolean);
  if (terms.length > 0) return terms.join(" · ");
  return counts.done > 0 ? `${counts.done} done` : "";
}
