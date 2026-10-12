import { Show, type JSX } from "solid-js";
import { Button, StatusFlag } from "~/ui";
import type { BranchState } from "../data";

/**
 * What a worktree thread has changed, as a readout you can open.
 *
 * A thread with no worktree renders nothing here — the branch read answers 404 for one, which is
 * the ordinary case and not an error.
 *
 * **It no longer owns the patch.** The chip used to fetch the branch itself and show the
 * diff in a modal over a raw `<pre>`. Both were wrong once the panel could hold a diff:
 * a patch is read *against* the conversation that produced it, and a dialog covers the
 * very transcript you are checking it against. So the chip is what it always looked
 * like — a diffstat — and pressing it opens the Changes surface beside the thread. The
 * merge and discard buttons went with the patch, because they are decisions about the
 * thing you are looking at.
 *
 * The fetch moved out too (`branchState.ts`), since the surface and the chip are two
 * views of one answer.
 *
 * **It also reports staleness, and staleness is the one thing here that is a warning.**
 * A diffstat says what this thread wrote; `behind` says how far the base has moved
 * underneath it since — which is what decides whether the MERGE button at the end of
 * the Changes surface lands cleanly or opens a conflict. It is the only figure on this
 * chip that spends an accent, and it spends one because it is the only one that is
 * about to cost the operator something. Everything else stays in the button's own grey.
 *
 * Reported on `behind > 0`, never on presence: the backend sends 0 both for "level with
 * the base" and for "could not tell", and neither is a thing to warn about.
 */
export function BranchChip(props: {
  branch: () => BranchState | null | undefined;
  onOpen: () => void;
}): JSX.Element {
  return (
    <Show when={props.branch()}>
      {(b) => (
        /* Sized to the chat header's other actions rather than to a toolbar chip —
           it is a control in that row, and a shorter one beside them reads as a
           different kind of thing. */
        <Button
          variant="ghost"
          leading="branch"
          onClick={props.onOpen}
          // Spelled out rather than left to the glyphs: the visible readout is mono
          // shorthand, and a screen reader hitting "+12 −3 · 4 BEHIND" reads arithmetic.
          aria-label={
            `Review this conversation's changes: ${b().filesChanged} file` +
            `${b().filesChanged === 1 ? "" : "s"} changed` +
            (b().behind > 0
              ? `, ${b().behind} commit${b().behind === 1 ? "" : "s"} behind ${b().baseRef}`
              : "")
          }
        >
          {b().branch} · {b().filesChanged} FILE
          {b().filesChanged === 1 ? "" : "S"} +{b().insertions} −{b().deletions}
          {/* Inside the button, after the diffstat, because it qualifies the diffstat:
              "this is what changed, and this is how much has changed under it". A
              separate control beside it would be a second thing to press for one fact.
              `aria-hidden` on the flag's own text — the button's label above already
              says it in words, and the flag would otherwise repeat it as shorthand. */}
          <Show when={b().behind > 0}>
            <span aria-hidden="true" class="contents">
              <StatusFlag status="warn" dot class="ml-1">
                {`${b().behind} BEHIND`}
              </StatusFlag>
            </span>
          </Show>
        </Button>
      )}
    </Show>
  );
}
