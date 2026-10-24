import { Show, type JSX } from "solid-js";
import { ConsoleGroup, Frames, Text } from "~/ui";
import { compactCount } from "~/lib/format";
import { asCompactionReason, compactionReasonCause } from "../compactionReason";
import type { ChatMessage } from "../model";
import { CompactionRow as Row } from "./CompactionRow";
import { CompactionSummary } from "./CompactionSummary";

/** Where the thread's earlier turns were folded into a summary — as a turn in the
 *  transcript, in flight and settled alike.
 *
 *  **A console group (§10.14), and the whole thing is machine voice (§2).** Nobody said
 *  this: the chassis did it *to* the model, and what the panel holds is the chassis' own
 *  record of the act — a cause, two figures, and the briefing the model now reads in place
 *  of everything above. So it is set in mono throughout, which does two jobs at once. It
 *  is the honest register for machine output, and it is what stops a checkpoint reading as
 *  one more contribution to the conversation: the fold is the one thing in the transcript
 *  that is *about* the transcript.
 *
 *  §10.14 reserves the group for readouts and warns off prose, and the exception is worth
 *  stating rather than assuming. A checkpoint is not an answer read start to finish — it
 *  is a fixed roster of named sections, and what the operator does with it is scan
 *  for the one they care about (*what did it keep about the decisions? the paths?*). That
 *  is the reading a ruled column is for. What the rule guards against is a frame drawn
 *  around ordinary content at the wrong density, and this is not that.
 *
 *  **One component for both states**, because they are one event and the operator watches
 *  it cross from one to the other. While the summarizer runs the panel carries a throbber
 *  and the summary as it is written; when `conversation.compacted` lands, the live turn is
 *  dropped and the settled one is seated at the fold boundary — a different place in the
 *  list, which is why the room scrolls to it rather than leaving the operator to find it.
 */
export function CompactionTurn(props: { message: ChatMessage }): JSX.Element {
  const m = () => props.message;
  const live = () => m().compaction;
  return (
    <div class="my-2 w-full">
      <Show when={live()} fallback={<Settled message={props.message} />}>
        {(c) => (
          <ConsoleGroup
            label="Compacting context"
            flush
            right={<Frames class="text-micro text-info shrink-0" />}
          >
            <div class="divide-line divide-y">
              <Row legend="Folding">
                <Text variant="code" tone="dim">
                  {c().messages} {c().messages === 1 ? "message" : "messages"}
                  <Show when={c().tokensEstimate > 0}>
                    {" · ~"}
                    {compactCount(c().tokensEstimate, true)} tokens
                  </Show>
                  {" · "}
                  {compactionReasonCause(c().reason)}
                </Text>
              </Row>
              {/* The model's working, not the checkpoint: raw text, unstripped and
                  unfenced, replaced wholesale when the real summary lands. It is here so
                  the pause has something in it — a throbber over a count tells the
                  operator that something is happening and never what. Bounded, because
                  it is arriving continuously and an unbounded one would push the
                  composer off the screen while they watched. */}
              <Show when={c().summary}>
                <Row legend="Writing">
                  <Text
                    variant="code"
                    tone="dim"
                    class="block max-h-40 overflow-hidden break-words whitespace-pre-wrap"
                  >
                    {c().summary}
                  </Text>
                </Row>
              </Show>
            </div>
          </ConsoleGroup>
        )}
      </Show>
    </div>
  );
}

/** The fold, once it has landed: what it cost, and what the model is left holding. */
function Settled(props: { message: ChatMessage }): JSX.Element {
  const m = () => props.message;
  const before = () => m().tokensBefore ?? 0;
  const after = () => m().tokensAfter ?? 0;
  const freed = () => Math.max(0, before() - after());
  const folded = () => m().foldedMessages ?? 0;
  return (
    <ConsoleGroup
      label="Context compacted"
      flush
      right={
        // The one figure worth the band's edge: what the fold bought. Everything else
        // about it is a row away, and a header listing all of them would be a second copy
        // of the panel set in smaller type.
        <Text variant="meta" tone="dim" class="tabular-nums">
          {/* Typed in sentence case; `meta` is what sets it in capitals. */}
          <Show when={freed() > 0} fallback={<>No room freed</>}>
            Freed ~{compactCount(freed(), true)}
          </Show>
        </Text>
      }
    >
      <div class="divide-line divide-y">
        <Row legend="Fold">
          <Text variant="code" tone="dim">
            <Show when={folded() > 0}>
              {folded()} {folded() === 1 ? "message" : "messages"}
              {" · "}
            </Show>
            {compactionReasonCause(
              asCompactionReason(m().compactionReason) ?? "threshold",
            )}
          </Text>
        </Row>
        {/* What the summary costs, and what it cost before — on the legend line of the
            thing they measure rather than in a row of their own. They had one, and a
            whole ruled row spending a `plate` legend on two figures is the shape §1.1
            calls out: a readout loud enough to be read as a section, saying less than the
            line it sits above.

            Guarded on the *pair*, because the backend always sends both and zero means
            "nothing to report" rather than a measured nothing. The guard is the
            `undefined`, not a `Show` inside the slot: an element is always truthy, so
            that would draw the slot around nothing. */}
        <Row
          legend="Summary"
          readout={
            before() > 0 || after() > 0 ? (
              <>
                ~{compactCount(after(), true)} · WAS ~
                {compactCount(before(), true)}
              </>
            ) : undefined
          }
        >
          {/* What the dimming above means, stated rather than addressed — the panel is a
              readout, and a row that turns round to say "your transcript" is the one line
              that breaks the register. The fact still has to be here: nothing else on
              screen distinguishes "those turns are gone" from "the model stopped reading
              them", and only one of those is true. */}
          <Text variant="micro" tone="dim">
            Dimmed turns above are no longer sent to the model — this summary
            stands in for them. The transcript keeps every one.
          </Text>
        </Row>
        {/* The sections are the backend's parse of this very checkpoint, not a second
            reading of it here — so what the operator reads is what the model was given.
            The fallback is the pre-parse rendering, kept for a checkpoint folded before
            the parse existed and for one whose text the summarizer drifted away from:
            those degrade to one keyless section rather than to nothing. */}
        <CompactionSummary
          sections={
            m().summarySections?.length
              ? (m().summarySections ?? [])
              : [
                  {
                    key: "",
                    body: m().content,
                    untrusted: false,
                    voice: "prose",
                  },
                ]
          }
          restored={m().restored ?? []}
        />
      </div>
    </ConsoleGroup>
  );
}
