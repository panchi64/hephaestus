import { Show, createMemo, type JSX } from "solid-js";
import { Button, ExpandableText, Icon, Panel, StatusDot, Text } from "~/ui";
import { relativeTime } from "~/lib/format";
import type { ChatSummary } from "../model";
import { runState, showThreadRecap } from "../recapVisibility";
import { runStateSpec } from "../runState";

/** About two lines of the reading register before MORE. */
const RECAP_PREVIEW_CHARS = 280;

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
 * What this thread was, for an operator who has just come back to it: the summary the
 * backend's utility model writes once a thread has been idle a while, collapsed to a
 * couple of lines with MORE.
 *
 * Two figures sit under it because the summary can't carry them: when the thread
 * opened, and the last run's outcome (a summary written before a failure won't say so).
 * A `Panel` rather than a `ConsoleGroup`, which is not for prose (§7).
 *
 * Absent with no summary yet, a thread quiet for less than `COLD_AFTER_MS`, or a run in
 * flight, where the live state is the answer.
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
              <Icon name="close" size={14} />
            </Button>
          }
        >
          {/* The reading register, not the interface one: this is a paragraph the
              operator reads, sitting above a transcript set at the same size, and
              dropping it to 13px chrome would make the one thing the band exists to
              say the smallest thing in it (§4). */}
          <ExpandableText
            text={s().workSummary ?? ""}
            limit={RECAP_PREVIEW_CHARS}
            variant="reading"
            tone="default"
            class="block"
          />
          <div class="mt-3 flex flex-wrap items-start gap-x-6 gap-y-1">
            <Cell label="Opened" value={relativeTime(s().createdAt)} />
            {/* Absent when the backend has nothing to say — the run registry is
                bounded, so a thread that plainly finished can carry no outcome, and a
                cell reading UNKNOWN would invent a state out of a missing record. */}
            <Show when={state()}>
              {(st) => (
                <Cell
                  label="Last run"
                  value={runStateSpec(st(), s().awaitingKind).readout}
                  mark={
                    <StatusDot
                      status={runStateSpec(st(), s().awaitingKind).status}
                    />
                  }
                />
              )}
            </Show>
          </div>
        </Panel>
      )}
    </Show>
  );
}
