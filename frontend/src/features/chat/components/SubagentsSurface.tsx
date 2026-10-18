import { For, Show, createMemo, createSignal, type JSX } from "solid-js";
import { Disclosure, EmptyState, Text, cx, toast } from "~/ui";
import {
  isLive,
  openSubagentThread,
  refreshSessions,
  stopSubagent,
  type Subagent,
} from "../data";
import { mainChat } from "../mainChat";
import { arrange, byUrgency } from "../subagentRoster";
import { LEDGER_COLUMNS, SubagentCard } from "./SubagentCard";
import { SubagentDetail } from "./SubagentDetail";
import { SurfaceBody } from "./surfaceChrome";

const HEADINGS = ["Agent", "Task", "Latest", "Ctx", "T+"];

/**
 * The sub-agents a thread has launched. The operator cannot talk to one, so this is
 * where they watch it work, read what it did, and see where it went wrong.
 *
 * - **Live and unsuccessful rows stay out; clean finishes fold away.** A failure hidden
 *   behind *Finished* would hide the main reason to open the panel.
 * - **Finished is collapsed every time it opens**, never remembered: a remembered open
 *   state would reopen a long session's whole history.
 * - **A row opens its sub-agent's view in place of the list.** A transcript unfolding
 *   under its row grew on every poll and pushed the next row from under the cursor.
 */
export function SubagentsSurface(props: {
  subagents: () => Subagent[];
  speaker: string;
  /** Re-read the cards — after a decision the operator just gave one of them. */
  onSettled?: () => void;
}): JSX.Element {
  const [openId, setOpenId] = createSignal<string | null>(null);

  // Resolved against the live list, not held: a captured card would keep the status
  // and context figures it had when it was clicked.
  const open = createMemo(
    () => props.subagents().find((s) => s.id === openId()) ?? null,
  );

  // Cancelled stays out with failed: a stopped sub-agent did not do its job either.
  const showing = (): Subagent[] =>
    props
      .subagents()
      .filter((s) => isLive(s) || s.status !== "done")
      .sort(byUrgency);
  const finished = (): Subagent[] =>
    props.subagents().filter((s) => s.status === "done");

  const stop = async (subagent: Subagent): Promise<void> => {
    if (!(await stopSubagent(subagent.runId)))
      toast.error("That agent could not be stopped.");
    props.onSettled?.();
  };

  const openThread = async (subagent: Subagent): Promise<void> => {
    if (subagent.opened) {
      mainChat().setCurrentId(subagent.conversationId);
      return;
    }
    const id = await openSubagentThread(subagent.conversationId);
    if (id === null) {
      toast.error("That agent could not be opened as a thread.");
      return;
    }
    refreshSessions();
    props.onSettled?.();
    mainChat().setCurrentId(id);
  };

  /** One ruled ledger (§10.14). The column legends only exist at the wide layout; the
   *  narrow one stacks its readout, so there are no columns to name. */
  const roster = (subagents: Subagent[]): JSX.Element => (
    <div class="border border-line-strong">
      <div
        aria-hidden="true"
        class={cx(
          LEDGER_COLUMNS,
          "hidden border-b border-line px-2 py-1 @2xl:grid",
        )}
      >
        <For each={HEADINGS}>
          {(heading, i) => (
            <Text
              variant="plate"
              tone="dim"
              class={i() === HEADINGS.length - 1 ? "text-right" : undefined}
            >
              {heading}
            </Text>
          )}
        </For>
      </div>
      <ul class="flex flex-col divide-y divide-line">
        <For each={arrange(subagents)}>
          {({ subagent, level }) => (
            <li>
              <SubagentCard
                subagent={subagent}
                level={level}
                onOpen={() => setOpenId(subagent.id)}
                onStop={() => void stop(subagent)}
                onOpenThread={() => void openThread(subagent)}
              />
            </li>
          )}
        </For>
      </ul>
    </div>
  );

  return (
    // The list scrolls inside; a sub-agent's view pins its header and scrolls only
    // its transcript.
    <SurfaceBody scroll={false}>
      {/* Unkeyed on purpose: every poll hands back new card objects, and a keyed `Show`
          would remount the transcript and lose its scroll position each time. A card
          closed out while open resolves to nothing and drops back to the list. */}
      <Show when={open()}>
        {(subagent) => (
          <SubagentDetail
            subagent={subagent()}
            onBack={() => setOpenId(null)}
            onSettled={props.onSettled}
          />
        )}
      </Show>

      {/* No title or count here: the pane's frame carries both. */}
      <Show when={!open()}>
        <div class="flex min-h-0 flex-col gap-2 overflow-y-auto">
          <Show
            when={showing().length > 0 || finished().length > 0}
            fallback={
              <EmptyState
                message="No sub-agents yet"
                hint={`When ${props.speaker} delegates, each automaton it launches appears here by callsign and class.`}
              />
            }
          >
            <Show when={showing().length > 0}>{roster(showing())}</Show>
          </Show>

          <Show when={finished().length > 0}>
            <Disclosure label={`Finished (${finished().length})`}>
              <div class="pt-1">{roster(finished())}</div>
            </Disclosure>
          </Show>
        </div>
      </Show>
    </SurfaceBody>
  );
}
