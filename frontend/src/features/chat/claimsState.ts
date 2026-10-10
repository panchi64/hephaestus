/**
 * The thread's claim-level reading, fetched once for the panel that shows it.
 *
 * Unlike the source inventory and the command log — both derived from the transcript —
 * this is a separate reading the backend stores sealed and serves on its own route. It
 * is fetched rather than derived because it is genuinely not in the transcript: it is
 * what a *second* model found when it read the answer against the sources the turn
 * retained.
 *
 * **Absent is the ordinary state.** Every non-research thread has none of this, as does
 * every research answer that cited nothing, so an empty read is never an error and never
 * blanks the message-level sources the panel already shows.
 *
 * The same `.latest` discipline `branchState.ts` keeps, for the same reason: a plain read
 * goes `undefined` for the length of a refetch, and the claims would blink out of the
 * panel at exactly the moment a turn finishes and the operator looks up.
 *
 * **Everything here is tagged with the thread it is about.** This controller outlives
 * every conversation the room shows, and the pass it drives takes a model call — long
 * enough for the operator to move on. Untagged, `.latest` shows the thread they left
 * until the next read lands, a write-back lands in whichever thread is current, and one
 * boolean lights (and then clears) a button that belongs to a different conversation.
 */

import { createResource, type Accessor } from "solid-js";
import { createInFlight } from "~/lib/inFlight";
import { settled } from "~/lib/resource";
import { extractAttributions, fetchAttributions } from "./data/attributions";
import {
  collectClaims,
  NO_CLAIMS,
  type ClaimInventory,
} from "./viewport/claimItems";

export interface ClaimsStateApi {
  claims: Accessor<ClaimInventory>;
  /** Ask for a reading of the latest answer — the retroactive path, for threads that
   *  finished before the pass existed and for a turn whose live pass degraded. */
  extract: () => void;
  extracting: Accessor<boolean>;
}

/**
 * `revision` is bumped by the caller when a turn settles, which is when the live pass
 * has just run. A research turn's claims therefore arrive on the next read rather than
 * mid-stream — the backend carries no event for them, and the `cited` citations that do
 * stream are the live signal that a reading landed.
 */
export function createClaimsState(
  conversationId: () => string | null,
  revision: () => number,
  enabled: () => boolean,
): ClaimsStateApi {
  // The fetched rows carry their conversation, the way the compaction toggle's do: a
  // resource keeps handing back its last value across a key change, and these rows are
  // claims *about a specific thread's answer*. Shown against another thread they are not
  // stale-but-harmless, they are wrong.
  const [rows, { mutate, refetch }] = createResource(
    () => {
      const id = conversationId();
      return id === null || !enabled()
        ? undefined
        : ([id, revision()] as const);
    },
    async ([id]) => ({ id, rows: await fetchAttributions(id) }),
  );
  const extracting = createInFlight<string>();

  const claims = (): ClaimInventory => {
    // `settled`, which is `.latest` with the unresolved cases spelled out: the value
    // survives a refetch, and an absent or failed read is the ordinary empty state
    // rather than a throw into whatever boundary sits above the panel.
    const latest = settled(rows);
    return latest && latest.id === conversationId()
      ? collectClaims(latest.rows)
      : NO_CLAIMS;
  };

  const extract = (): void => {
    const id = conversationId();
    if (id === null) return;
    // Claimed per thread rather than by one boolean: a pass on one conversation must not
    // refuse a pass on another, nor clear its flag when it finishes.
    void extracting.run(id, () =>
      extractAttributions(id)
        // Written straight into the resource rather than triggering a refetch: the POST
        // already answers with the whole thread's reading, so re-reading it would be a
        // second round trip for bytes just received.
        //
        // Both arms check the room first. A pass holds a model call, and `mutate` writes
        // the resource the *current* thread reads — so a write-back after the operator
        // moved on would file this thread's claims under theirs. The refetch arm is the
        // same hazard by another route: it would re-read whichever thread is bound now,
        // which is not the one whose pass just failed. The thread that was left picks
        // the reading up from its own fetch when it is opened again.
        .then((fresh) => {
          if (conversationId() === id) mutate({ id, rows: fresh });
        })
        .catch(() => {
          if (conversationId() === id) void refetch();
        }),
    );
  };

  // Per-thread, composed here from the reactive bound id — the same pairing the room
  // makes for a fold in flight.
  const isExtracting = (): boolean => {
    const id = conversationId();
    return id !== null && extracting.has(id);
  };

  return { claims, extract, extracting: isExtracting };
}
