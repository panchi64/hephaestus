import type { ChatMessage } from "../model";

/** The infix `nextId` gives every message and block minted on the client. */
const LIVE = "-live-";

/**
 * Carry a just-finished turn's identity across the reseat that follows it, so the store
 * update lands in place instead of replacing the turn.
 *
 * The backend's rows arrive under their own ids, and `reconcile` keyed on `id` treats a
 * renamed message as a new one, which would remount the turn as the operator starts
 * reading it.
 *
 * Returns the `[index, backendId]` renames to apply to the live store before
 * reconciling, and gives each renamed message's blocks in `next` the id of the live
 * block they line up with (block ids are render keys only). Returns no renames unless
 * the two paths line up: same length, same roles, and every differing id a live one.
 */
export function adoptLiveIdentity(
  live: readonly ChatMessage[],
  next: ChatMessage[],
): [number, string][] {
  if (live.length !== next.length) return [];
  const renames: [number, string][] = [];
  for (let i = 0; i < live.length; i++) {
    const current = live[i];
    const settled = next[i];
    if (current.id === settled.id) continue;
    if (!current.id.includes(LIVE) || current.role !== settled.role) return [];
    renames.push([i, settled.id]);
  }
  for (const [i] of renames) keepBlockIds(live[i], next[i]);
  return renames;
}

/** Pair a tool call with the same call, anything else by kind in order. The live turn
 *  holds blocks a reload does not (injected context among them), so those are skipped
 *  rather than ending the match. A block with no partner keeps its own id. */
function keepBlockIds(live: ChatMessage, next: ChatMessage): void {
  const from = live.blocks ?? [];
  const taken = new Set<number>();
  let at = 0;
  for (const block of next.blocks ?? []) {
    const match =
      block.kind === "tool"
        ? from.findIndex(
            (b, i) =>
              !taken.has(i) && b.kind === "tool" && b.tool.id === block.tool.id,
          )
        : from.findIndex(
            (b, i) => i >= at && !taken.has(i) && b.kind === block.kind,
          );
    if (match < 0) continue;
    block.id = from[match].id;
    taken.add(match);
    at = Math.max(at, match + 1);
  }
}
