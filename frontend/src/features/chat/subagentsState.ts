/**
 * The thread's sub-agents, kept current for as long as any of them is working.
 *
 * Created once by the room and handed to whoever wants it, the same as the branch beside
 * it: the header does not want it today, but the panel and the surface registry both do,
 * and two `createResource`s over one endpoint are two answers that disagree while either
 * is in flight.
 *
 * **It polls, and that is the design rather than a shortcut.** A sub-agent outlives every
 * stream there is to hang it on — its own ends when it does, and the launching turn's ends
 * long before that — so "tell me when this changes" would need a third stream fanning the
 * others in, held open per conversation for as long as anyone might look. Re-asking a
 * local endpoint for a short list is the same information without that machine, and it is
 * also the only version that is right after a restart.
 *
 * **Only while something is live.** The interval starts when a sub-agent is working and
 * stops when the last one finishes, so a thread whose sub-agents are all done — which is
 * most threads, most of the time — costs exactly one request when it is opened. A card
 * that has finished cannot change again.
 */

import {
  createEffect,
  createSignal,
  createResource,
  onCleanup,
} from "solid-js";
import { settled } from "~/lib/resource";
import {
  fetchSubagents,
  isLive,
  type Subagent,
  type SubagentsRead,
} from "./data";

/** How often to re-ask while something is working. Fast enough that a context ring and a
 *  status read as live, slow enough to be invisible: the answer is a short list from a
 *  local process, and nothing here is worth a stream. */
const POLL_MS = 3000;

export interface SubagentsApi {
  /** What consumers read: the last answer that actually came back *for this thread*. */
  latest: () => Subagent[];
  refetch: () => void;
}

/**
 * `revision` is bumped by the caller when a turn settles — the moment a launch is most
 * likely to have just happened, so the first card appears without waiting for a tick.
 *
 * **A signal of its own, not the resource's value.** A plain read goes `undefined` for the
 * length of every re-fetch, which on a three-second poll means the panel emptying and
 * refilling continuously while the operator is reading it — and a read that *failed* must
 * not empty it either, since a dropped request says nothing about what the thread has
 * running. So the last answer that actually came back is held here, and a failure leaves
 * it standing — but only for the thread it was read for: each answer is tagged with its
 * thread, and a switch reads empty until the new thread's own answer lands, rather than
 * showing the cards of the one just left.
 */
export function createSubagentsState(
  conversationId: () => string | null,
  revision: () => number,
): SubagentsApi {
  const [subagents, { refetch }] = createResource(
    () => {
      const id = conversationId();
      return id === null ? undefined : ([id, revision()] as const);
    },
    async ([id]) => ({ id, read: await fetchSubagents(id) }),
  );
  const [known, setKnown] = createSignal<{
    id: string;
    list: Subagent[];
  } | null>(null);

  /** The latest answer, if it is for the thread on screen. */
  const current = (): SubagentsRead | undefined => {
    const held = settled(subagents);
    return held && held.id === conversationId() ? held.read : undefined;
  };

  createEffect(() => {
    const read = current();
    const id = conversationId();
    if (read?.ok && id) setKnown({ id, list: read.subagents });
  });

  const latest = (): Subagent[] => {
    const held = known();
    return held && held.id === conversationId() ? held.list : [];
  };

  createEffect(() => {
    // Reading the resource here is what makes this self-sustaining: every settled read —
    // including a failed one, which comes back as a fresh object — re-runs the effect and
    // re-arms the timer, so long as something is still working. A read that failed keeps
    // the poll going against what was last known, because giving up on the one request
    // that could correct it is the last thing to do about a dropped request.
    const read = current();
    if (read === undefined) return;
    const live = read.ok ? read.subagents : latest();
    if (!live.some(isLive)) return;
    const timer = setTimeout(() => void refetch(), POLL_MS);
    onCleanup(() => clearTimeout(timer));
  });

  return { latest, refetch: () => void refetch() };
}
