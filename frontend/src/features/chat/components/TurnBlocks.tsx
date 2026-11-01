import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  on,
  untrack,
  type JSX,
} from "solid-js";
import { Reveal } from "~/ui";
import type { AssistantBlock } from "../model";
import {
  groupBlocks,
  layoutItemKey,
  liveToolGroupIds,
  planTurnLayout,
  workLogStateKey,
  type LayoutItem,
} from "../blocks";
import type { ViewItem } from "../viewport/viewItems";
import {
  BlockRow,
  type ChipLookupEntry,
  type RowHandlers,
  type TopSpacing,
} from "./BlockRow";
import { WorkLog } from "./WorkLog";

/** Render an assistant turn as its ordered, interleaved blocks — the agent's
 *  true think → tool → text → … sequence — its process gathered into work logs,
 *  each a trunk of rows between the answers. */
export function TurnBlocks(
  props: {
    blocks: AssistantBlock[] | undefined;
    streaming?: boolean;
    /** Expand-all / collapse-all from the turn header. */
    forceOpen?: boolean;
    /** The conversation's consolidated View list, for the inline chips' own
     *  version/time/NEW metadata — see `RowHandlers.chipLookup`. */
    viewItems?: () => ViewItem[];
    /** The "seen through" pointer — see `RowHandlers.seenIndex`. */
    seenKey?: () => string | null;
  } & Omit<RowHandlers, "chipLookup" | "seenIndex">,
): JSX.Element {
  // Memoized so a text/thinking delta (which doesn't change block *structure*)
  // doesn't re-group/re-plan, and so `activeIds` reuses the same grouping rather
  // than recomputing it — one structural pass per real change, not per token.
  const groups = createMemo(() => groupBlocks(props.blocks));
  const layout = createMemo(() =>
    planTurnLayout(groups(), { streaming: props.streaming }),
  );

  /* `<For>` is reference-keyed and `planTurnLayout` mints fresh objects on every
     call, including when `streaming` flips at the end of a run. Iterating the
     plan directly would redraw the whole turn on every recompute.

     So iterate stable keys, which compare by value, and look each item up through
     a memo that keeps the row's props live: a row is created once, and only new or
     regrouped rows move. */
  const keys = createMemo(() => layout().map(layoutItemKey));
  const byKey = createMemo(
    () =>
      new Map<string, LayoutItem>(
        layout().map((item) => [layoutItemKey(item), item] as const),
      ),
  );
  const indexOfKey = (key: string): number => keys().indexOf(key);
  // One key -> {item, index} lookup for the turn's inline chips, built once per
  // View-list change (not per chip) — mirrors `ViewTimelineRail`'s own use of
  // `classifyViewItem`/`viewItemVersionLabel`/`viewItemTimeLabel` for the same
  // data. `seenIndex` is the "seen through" key's position in that same
  // chronological list (-1 when unset or no longer present, e.g. a rewind).
  const chipLookup = createMemo(() => {
    const items = props.viewItems?.() ?? [];
    const map = new Map<string, ChipLookupEntry>();
    items.forEach((item, index) => map.set(item.key, { item, index }));
    return map;
  });
  const seenIndex = createMemo(() => {
    const items = props.viewItems?.() ?? [];
    const key = props.seenKey?.() ?? null;
    return key ? items.findIndex((i) => i.key === key) : -1;
  });
  // While streaming, the trailing group is live by position and any group with a call
  // in flight is live by state. Parallel calls make those different groups, so naming
  // only the tail would un-light the rest of a batch that is still running.
  const activeIds = createMemo(() => {
    const gs = groups();
    if (!props.streaming || !gs.length) return new Set<string>();
    const ids = liveToolGroupIds(gs);
    ids.add(gs[gs.length - 1].id);
    return ids;
  });

  // Each work log's open state lives here, keyed by `workLogStateKey`, so it survives
  // a rebuilt `layout()` and the reseat after a run. Toggling the turn-level
  // expand/collapse-all wipes the per-log overrides so the global control re-takes
  // command of every log.
  const [openLogs, setOpenLogs] = createSignal<Record<string, boolean>>({});
  createEffect(
    on(
      () => props.forceOpen,
      () => setOpenLogs({}),
      { defer: true },
    ),
  );
  const logOpen = (id: string): boolean =>
    openLogs()[id] ?? props.forceOpen ?? false;
  const toggleLog = (id: string): void => {
    setOpenLogs({ ...openLogs(), [id]: !logOpen(id) });
  };

  /* Whether a row should materialize as it arrives.

     Latched at mount, and deliberately not reactive. A live turn's blocks appear
     one at a time, and each one popping into place would jolt the reply. A turn
     read from history mounts all of its rows at once, and animating those would
     be a whole transcript moving on load, which §8 forbids. Read reactively, the
     wrapper element would change when the run ended and flash the entire turn. */
  const revealRows = untrack(() => Boolean(props.streaming));

  return (
    <div>
      <For each={keys()}>
        {(key) => {
          // Narrowed per kind, so each arm reads its own shape without casts.
          // Both stay live: the item object behind a key is replaced on every
          // recompute, and reading it through these is what lets the row update
          // in place instead of being rebuilt.
          const item = () => byKey().get(key);
          const log = () => {
            const it = item();
            return it?.type === "worklog" ? it : undefined;
          };
          const group = () => {
            const it = item();
            return it?.type === "group" ? it.group : undefined;
          };
          const top = (): TopSpacing =>
            indexOfKey(key) === 0 ? "none" : "gap";
          const row = (
            <Show
              when={log()}
              fallback={
                <Show when={group()}>
                  {(g) => (
                    <BlockRow
                      group={g()}
                      active={activeIds().has(g().id)}
                      streaming={props.streaming}
                      top={top()}
                      forceOpen={props.forceOpen}
                      onResolveHostCommands={props.onResolveHostCommands}
                      onOpenInView={props.onOpenInView}
                      chipLookup={chipLookup()}
                      seenIndex={seenIndex()}
                    />
                  )}
                </Show>
              }
            >
              {(l) => (
                <WorkLog
                  entries={l().entries}
                  open={logOpen(workLogStateKey(l()))}
                  onToggle={() => toggleLog(workLogStateKey(l()))}
                  top={top()}
                  forceOpen={props.forceOpen}
                  /* A live call is pinned inside the log rather than lifted out
                     of it, so the trunk's light has to reach in here too. */
                  activeIds={activeIds()}
                  streaming={props.streaming}
                  onResolveHostCommands={props.onResolveHostCommands}
                  onOpenInView={props.onOpenInView}
                  chipLookup={chipLookup()}
                  seenIndex={seenIndex()}
                />
              )}
            </Show>
          );
          /* A plain fade, not a rise: a work log is a continuous trunk of
             hairlines, and anything that moved would drag the trunk with it. */
          return revealRows ? <Reveal>{row}</Reveal> : row;
        }}
      </For>
    </div>
  );
}
