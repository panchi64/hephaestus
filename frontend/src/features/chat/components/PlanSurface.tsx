import { For, Show, type JSX } from "solid-js";
import { EmptyState, Markdown, Text } from "~/ui";
import type { PlanDocument, PlanStatus } from "../model";
import { SurfaceBody, SurfaceSection } from "./surfaceChrome";

/** How each state reads, and how loudly. `pending` is the only one that is a question;
 *  the rest are a record of one already answered, and record-shaped states stay quiet. */
const STATUS: Record<
  PlanStatus,
  { label: string; tone: "warn" | "nominal" | "alert" | "idle" }
> = {
  pending: { label: "Awaiting your approval", tone: "warn" },
  approved: { label: "Approved", tone: "nominal" },
  revising: { label: "Revising", tone: "idle" },
  denied: { label: "Rejected", tone: "alert" },
};

/** The status as this build can word it. A backend that adds a fifth reads as a plain
 *  state name rather than throwing on the lookup — the document beneath it is the point
 *  of the panel, and losing all of it to an unrecognised flag would be the wrong trade.
 *
 *  Exported for the pane header, which is where the status is printed: it is a fact
 *  about the pane, like every other surface's meta, not a line of the document. */
export const planStatusOf = (status: PlanStatus): (typeof STATUS)[PlanStatus] =>
  STATUS[status] ?? { label: String(status).toUpperCase(), tone: "idle" };

/**
 * **The plan the agent wants to carry out** — the document, read at a panel's width.
 *
 * This is what the whole of plan mode exists to produce, and the operator decides on the
 * strength of it alone, so it gets the room to be read in rather than a card's worth of
 * summary.
 *
 * **The decision is not here.** It used to be: this panel rendered its own
 * `ApprovalPanel`, on the reasoning that a document should be answered where it is read.
 * What that actually bought was one approval answered in a place no other approval lives,
 * a host with no panel (a compare pane) unable to answer at all, and a park split across
 * two surfaces that has to resume on one body. The answer moved back to `ParkDock` with
 * every other approval, and the dock carries a button that opens this panel — so the
 * reading and the deciding are one gesture apart instead of one of them being homeless.
 *
 * What this owes the operator instead is **saying where the plan is up to**. The status
 * flag rides the pane header (`SURFACE_META`); `revising` is a wait on a whole model
 * turn, so it is also written out here, where a dimmed flag alone left it looking like
 * the interface had lost the buttons.
 *
 * **Dense prose, on a measure.** The pane is a column beside the transcript, so the
 * headings step down one rung (a 28px h1 was as loud as the pane's own title at 320px)
 * and the text holds a readable measure when the pane is dragged wide.
 */
export function PlanSurface(props: {
  plan: () => PlanDocument | null;
  speaker: string;
}): JSX.Element {
  return (
    <SurfaceBody>
      <Show
        when={props.plan()}
        keyed
        fallback={
          <EmptyState
            icon="note"
            message="No plan yet"
            hint={`In plan mode, the plan ${props.speaker} proposes lands here for you to read before approving.`}
          />
        }
      >
        {(plan) => (
          <div class="flex max-w-prose flex-col gap-3">
            <div class="flex flex-col gap-1">
              <Text variant="readout" tone="bright">
                {plan.title}
              </Text>
              {/* The one status that is a wait rather than a record. Written out, because
                  the gap between asking for changes and the next draft is a whole model
                  turn and silence over it reads as something having gone wrong. */}
              <Show when={plan.status === "revising"}>
                <Text variant="micro" tone="dim">
                  The agent is writing the next draft — it will be put to you
                  again when it is ready.
                </Text>
              </Show>
            </div>

            <Markdown dense>{plan.body}</Markdown>

            <Show when={plan.steps.length > 0}>
              <SurfaceSection label="Steps" count={plan.steps.length}>
                {/* Numbered, because the order is part of what is being agreed — and
                    because on approval these become the thread's task list in exactly
                    this order. */}
                <ol class="flex list-decimal flex-col gap-1 pl-5">
                  <For each={plan.steps}>
                    {(step) => (
                      <li>
                        <Text variant="body">{step}</Text>
                      </li>
                    )}
                  </For>
                </ol>
              </SurfaceSection>
            </Show>
          </div>
        )}
      </Show>
    </SurfaceBody>
  );
}
