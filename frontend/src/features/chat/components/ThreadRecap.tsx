import { Show, createMemo, type JSX } from "solid-js";
import { Button, Panel, StatusDot, Text } from "~/ui";
import { relativeTime } from "~/lib/format";
import type { ChatSummary } from "../model";
import { runState, showThreadRecap } from "../recapVisibility";
import { runStateSpec } from "../runState";

/** One legend over one value — the whole vocabulary of the footer. */
function Cell(props: {
  label: string;
  value: JSX.Element;
  /** A state mark before the value, for the one cell that carries a state. */
  mark?: JSX.Element;
}): JSX.Element {
  return (
    <div class="flex min-w-0 flex-col gap-0.5">
      {/* `plate` names the cell, `meta` reports what is in it — the legend/readout
          split (§4), which is also what keeps two lines of mono from reading as one
          block of grey. */}
      <Text variant="plate" tone="dim">
        {props.label}
      </Text>
      <span class="flex min-w-0 items-center gap-1.5">
        {props.mark}
        <Text variant="meta" tone="default" class="min-w-0 truncate">
          {props.value}
        </Text>
      </span>
    </div>
  );
}

/**
 * What this thread was, for an operator who has just come back to it.
 *
 * **It is a summary now, and it used to be a row of figures.** Age, turn count
 * and the branch diffstat were all things the interface could already answer — the
 * rail says when, the header chip says the branch — so the band spent a strip of
 * the screen restating them and answered the actual re-entry question, *what was I
 * doing here*, not at all. That question needs prose, and prose is not something the
 * client can derive: the backend's utility model writes it once a thread has been
 * idle a while, replaces it wholesale each time, and the band renders whatever the
 * newest one says.
 *
 * **Two figures survived, and both for the same reason** — they are facts about the
 * thread that the summary cannot carry and nothing else on screen says. `OPENED` is
 * when the thread began, which is the one thing that places it among the others; the
 * last run's outcome is what says a thread ended badly, and a summary written before
 * the failure would not mention it.
 *
 * **It is a `Panel`, not a `ConsoleGroup`.** The group is for ruled rows and fixed
 * cells read by scanning, and §7 is explicit that it is not for prose — which is
 * exactly what the band now leads with. A panel is the default container and the
 * right one the moment the content became a paragraph.
 *
 * **Absent unless it has something to say.** No summary yet (the ordinary state of a
 * thread still being worked on), a thread quiet for less than `COLD_AFTER_MS`, or a
 * run in flight — in that last case the live state is the answer, and a recap of the
 * previous turn would be reporting the past over the present.
 */
export function ThreadRecap(props: {
  /** The thread, as the shared session list already knows it. */
  summary: () => ChatSummary | undefined;
  /** True while a run is driving this thread — a recap is for a thread at rest. */
  streaming: () => boolean;
  /** Put it away for this visit. */
  onDismiss: () => void;
}): JSX.Element {
  /** The thread's state at rest — the same derivation `showThreadRecap` runs, read from
   *  the one place that owns it rather than resolved again here. */
  const state = createMemo(() => {
    const s = props.summary();
    return s ? runState(s) : undefined;
  });

  const cold = createMemo(() =>
    showThreadRecap(props.summary(), {
      streaming: props.streaming(),
      now: Date.now(),
    }),
  );

  return (
    <Show when={cold() && props.summary()}>
      {(s) => (
        <Panel
          label="Thread recap"
          class="mb-3"
          meta={
            <Button
              variant="ghost"
              size="sm"
              aria-label="Dismiss thread recap"
              onClick={() => props.onDismiss()}
            >
              ✕
            </Button>
          }
        >
          {/* The reading register, not the interface one: this is a paragraph the
              operator reads, sitting above a transcript set at the same size, and
              dropping it to 13px chrome would make the one thing the band exists to
              say the smallest thing in it (§4). */}
          <Text variant="reading" tone="default" class="block">
            {s().workSummary}
          </Text>
          <div class="mt-3 flex flex-wrap items-start gap-x-6 gap-y-1">
            <Cell label="Opened" value={relativeTime(s().createdAt)} />
            {/* Absent when the backend has nothing to say — the run registry is
                bounded, so a thread that plainly finished can carry no outcome, and a
                cell reading UNKNOWN would invent a state out of a missing record. */}
            <Show when={state()}>
              {(st) => (
                <Cell
                  label="Last run"
                  value={runStateSpec(st()).readout}
                  mark={<StatusDot status={runStateSpec(st()).status} />}
                />
              )}
            </Show>
          </div>
        </Panel>
      )}
    </Show>
  );
}
