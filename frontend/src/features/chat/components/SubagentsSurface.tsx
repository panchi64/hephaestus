import { For, Show, createMemo, createSignal, type JSX } from "solid-js";
import { Disclosure, EmptyState, toast } from "~/ui";
import {
  isLive,
  openSubagentThread,
  refreshSessions,
  stopSubagent,
  type Subagent,
} from "../data";
import { mainChat } from "../mainChat";
import { SubagentCard } from "./SubagentCard";
import { SubagentDetail } from "./SubagentDetail";
import { SurfaceBody } from "./surfaceChrome";

/** Waiting on the operator first, then working, then whatever went wrong.
 *
 *  Deliberately not chronological. A list ordered by when things started puts the one
 *  thing that is blocked on the operator wherever it happens to fall, which is the one
 *  ordering guaranteed not to surface it. */
const RANK: Record<string, number> = {
  blocked: 0,
  running: 1,
  failed: 2,
  cancelled: 3,
};

function byUrgency(a: Subagent, b: Subagent): number {
  const rank = (RANK[a.status] ?? 9) - (RANK[b.status] ?? 9);
  return rank !== 0 ? rank : b.startedAt.localeCompare(a.startedAt);
}

interface Placed {
  subagent: Subagent;
  level: number;
}

/** Lay the cards out depth-first, each directly beneath its parent. A card whose parent
 *  is not among them — the viewed thread itself, or one filtered into the other roster —
 *  is top level. */
function arrange(cards: Subagent[]): Placed[] {
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
  for (const card of cards) walk(card, 0);
  return placed;
}

/**
 * The sub-agents a thread has launched, and what they are doing.
 *
 * The operator cannot talk to a sub-agent — there is nowhere to type — so this is the
 * whole of their relationship with one: watch it work, read what it did, and see where
 * it went wrong. That shapes every choice here.
 *
 * **Three tiers, and the third is the interesting one.** Live sub-agents are the reason
 * the panel is open, so they are simply there. A sub-agent that *failed* stays with them
 * rather than folding away, because "see if anything went wrong" is the main reason to
 * come here at all and a failure hidden one click deep behind something labelled
 * *Finished* works against exactly that. Everything that finished cleanly goes into a
 * collapsed accordion: it is kept — a transcript is the record of what happened, and the
 * question about it is usually asked later — but a session's worth of successes between
 * the operator and the two cards they care about is a panel they stop opening.
 *
 * **Collapsed by default, every time.** Not remembered, deliberately: the accordion
 * exists to keep the panel readable, and a remembered *expanded* state would reopen a
 * long session's whole history the next time the panel is opened.
 *
 * **A card opens its sub-agent's own view, and the list gives way to it.** The transcript
 * used to unfold underneath the card instead, which made every row below it move — and
 * since a live transcript grows on every poll, the row the operator wanted next kept
 * sliding away from the cursor. See `SubagentDetail`.
 */
export function SubagentsSurface(props: {
  subagents: () => Subagent[];
  /** Re-read the cards — after a decision the operator just gave one of them. */
  onSettled?: () => void;
}): JSX.Element {
  const [openId, setOpenId] = createSignal<string | null>(null);

  // Resolved against the live list rather than held as an object: the panel re-reads
  // these while anything is working, and a captured card would go on showing the status
  // and the context figures it had when it was clicked.
  const open = createMemo(
    () => props.subagents().find((s) => s.id === openId()) ?? null,
  );

  // Failed and cancelled sit with the live ones — see the note above. `cancelled` is
  // there for the same reason in a quieter register: a sub-agent the operator stopped,
  // or one a restart closed out, did not do its job either.
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

  /** One ruled roster (design §7, §10.14): a frame of flush rows, never a grid of
   *  cards. It is a list read down a column of handles, so it stays one column at any
   *  pane width — two would put the scan in two places. */
  const roster = (subagents: Subagent[]): JSX.Element => (
    <ul class="flex flex-col divide-y divide-line border border-line-strong">
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
  );

  return (
    // Not the body's own scroll: the list scrolls inside, and a sub-agent's view pins
    // its header while only its transcript scrolls.
    <SurfaceBody scroll={false}>
      {/* One sub-agent, or all of them — never both. A sub-agent that is closed out
          while its view is open (a thread switch, a cancel) resolves to nothing and
          drops back to the list rather than holding a stale card on screen.

          Unkeyed, deliberately: every poll hands back freshly built cards, so a keyed
          `Show` would see a new object every few seconds and tear the whole view down
          and build it again — remounting the transcript, losing its scroll position and
          re-reading it from scratch, which is the exact instability this pane was made
          to stop. The accessor keeps the same view alive and lets the new figures
          through it. */}
      <Show when={open()}>
        {(subagent) => (
          <SubagentDetail
            subagent={subagent()}
            onBack={() => setOpenId(null)}
            onSettled={props.onSettled}
          />
        )}
      </Show>

      {/* No title row and no count: the pane's frame carries both now, so they are
          there whichever surface is in the pane and whether or not a sub-agent's own
          view is open over the list. */}
      <Show when={!open()}>
        <div class="flex min-h-0 flex-col gap-2 overflow-y-auto">
          <Show
            when={showing().length > 0 || finished().length > 0}
            fallback={
              <EmptyState
                message="No sub-agents yet"
                hint="Work the agent hands to a sub-agent shows up here while it happens."
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
