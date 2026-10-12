/**
 * The viewport pane, as one thing.
 *
 * The collapsible, resizable region beside the conversation — where documents and live
 * previews mount — is a single concern with a lot of surface: what it holds, whether it
 * is open, how wide it is, which version is pinned, how many items have arrived unseen,
 * whether it renders as an aside or as a full-screen sheet, and where focus goes when it
 * closes. All of that was inline in the chat screen, where it made up a third of the file
 * and interleaved with the transcript and the composer. None of it is about a
 * conversation; all of it is about a panel.
 *
 * **The dragged width is global; everything else is per conversation.** A manual close on
 * one thread must not be undone by a different thread's auto-open, so the layout, the
 * pinned version, the PREVIEW/CODE tab, the font size, wrap, fullscreen and the
 * seen-through pointer all go through `useViewportPersistence` keyed by thread. The width
 * is a genuine cross-thread preference and is the one thing that does not.
 *
 * **What the panel holds is now a layout rather than a flag.** Openness is derived from
 * it — there is no separate `open` to disagree with — and closing keeps the arrangement
 * so reopening restores what the operator was looking at rather than a default. Today
 * that layout only ever holds the View; the machinery underneath it does not care, which
 * is the point.
 *
 * Refs are handed back rather than taken: the row the pane shares width with, and the
 * header button focus returns to when a sheet closes, are both rendered by the screen.
 */

import {
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
  type Accessor,
} from "solid-js";
import { createPanelResize, observeAvailableWidth } from "./panelResize";
import type {
  ChatMessage,
  HostCommand,
  PlanDocument,
  ViewSnapshotRef,
} from "./model";
import type { BranchState, Subagent } from "./data";
import type { TaskItem } from "~/lib/stream/events";
import {
  emptyLayout,
  focusInStack,
  hasSurface,
  isEmpty,
  openSurface,
  panelSurfacesOf,
  resizeSplit,
  retainAvailable,
  surfacesOf,
  toggleSurface as toggleLayout,
  type TilingContext,
  type ViewportLayout,
} from "./viewport/layout";
import { panelBox, widenFor } from "./viewport/viewportWidth";
import { arrivalClaims, createSurfaceSources } from "./viewport/surfaceSources";
import {
  DEFAULT_VIEW_SURFACE,
  useViewportPersistence,
  type ViewportPersistedState,
  type ViewSurfaceState,
} from "./viewport/persistence";
import { shapeOf, SURFACE_IDS, type SurfaceId } from "./viewport/surfaces";
import { collectCommands } from "./viewport/commandItems";
import {
  buildInventory,
  collectSources,
  type SourceInventory,
} from "./viewport/sourceItems";
import {
  claimAutoOpen,
  collectViewItems,
  type ViewItem,
} from "./viewport/viewItems";

/** What the pane needs from the room's stream — narrowed to the three reads, so a
 *  panel can be reasoned about without the whole streaming controller. */
export interface ViewportSource {
  messages: ChatMessage[];
  /** Whether a run is streaming into the thread. */
  sending: Accessor<boolean>;
  snapshots: Accessor<ViewSnapshotRef[]>;
  tasks: Accessor<TaskItem[]>;
  plan: Accessor<PlanDocument | null>;
  branch: () => BranchState | null | undefined;
  refetchBranch: () => void;
  /** The sub-agents this thread has launched, for the Sub-agents surface. */
  subagents: Accessor<Subagent[]>;
  /** Re-read them — after the operator answers one that was waiting on them. */
  refetchSubagents: () => void;
  toggleSnapshotKeeper: (snapshotId: string, keeper: boolean) => Promise<void>;
}

export interface ChatViewport {
  state: () => ViewportPersistedState;
  patch: (next: Partial<ViewportPersistedState>) => void;
  /** The View surface's own preferences, and the way to change them. Consumers read
   *  these rather than reaching into `state().surfaces`, so where a surface's state
   *  is kept stays this module's business. */
  viewState: () => ViewSurfaceState;
  patchView: (next: Partial<ViewSurfaceState>) => void;
  items: Accessor<ViewItem[]>;
  /** The thread's task list, for the Tasks surface. */
  tasks: Accessor<TaskItem[]>;
  /** The plan it is working to, for the Plan surface — which reads it and nothing else;
   *  a submitted plan is answered in the dock with every other approval. */
  plan: Accessor<PlanDocument | null>;
  /** The thread's branch, for the Diff surface. */
  branch: () => BranchState | null | undefined;
  refetchBranch: () => void;
  /** The sub-agents this thread has launched, for the Sub-agents surface. */
  subagents: Accessor<Subagent[]>;
  /** Re-read them — after the operator answers one that was waiting on them. */
  refetchSubagents: () => void;
  /** Every command the thread has run, for the Commands surface. Derived from the
   *  transcript's terminal blocks, so it needs nothing from the room. */
  commands: Accessor<HostCommand[]>;
  /** Every source the thread has read, folded and shelved, for the Sources surface.
   *  Derived from the transcript's citations for the same reason `commands` is. */
  sources: Accessor<SourceInventory>;
  /** Whether a surface has anything to show — which header buttons exist. */
  available: (id: SurfaceId) => boolean;
  /** The first surface in registry order that has anything to show, or `undefined` on a
   *  thread that has nothing at all. The header row's leftmost button, and what an empty
   *  panel opens onto — one derivation, because those two must be the same surface. */
  firstAvailable: () => SurfaceId | undefined;
  /** What the panel is actually showing — the stored arrangement with every surface that
   *  has nothing in it taken out, or `null` when that leaves nothing. What the host
   *  renders; `state().layout` is the record, which can name more than this. */
  layout: Accessor<ViewportLayout | null>;
  /** Whether a surface is currently in the layout. */
  isOpen: (id: SurfaceId) => boolean;
  /** Put a surface on screen beside whatever is already open, bring it to the front of
   *  the stack it shares, and focus it — opening the panel if it is shut. What a control
   *  elsewhere in the room uses to send the operator to a surface ("Read the plan" in the
   *  approval dock); `toggleSurface` is the header button's act and would *close* the
   *  surface it was asked to show. */
  showSurface: (id: SurfaceId) => void;
  /** Bring a surface to the front of the tabbed pane it shares, and focus it. */
  revealSurface: (id: SurfaceId) => void;
  /** Close one surface from its own chrome. */
  closeSurface: (id: SurfaceId) => void;
  /** Drag a split's divider — `path` addresses it, `ratio` is `a`'s share. */
  adjustSplit: (path: readonly ("a" | "b")[], ratio: number) => void;
  /** Which surface the operator is working in. Surface-scoped key bindings gate on
   *  it, and it is what a pane's chrome marks as current. */
  focusedSurface: Accessor<SurfaceId | null>;
  setFocusedSurface: (id: SurfaceId) => void;
  /** Put a surface on screen, or take it off. Closing the last one closes the
   *  panel, since a panel showing nothing is a panel that is shut. */
  toggleSurface: (id: SurfaceId) => void;
  /** Flip between the aside and the full-screen sheet, keeping the keyboard
   *  where it was — see the implementation for why that is not automatic. */
  toggleFullscreen: () => void;
  /** Whether there is anything at all to show — the panel's enablement. */
  hasContent: () => boolean;
  /** Rendered as the desktop aside. */
  asideOpen: () => boolean;
  /** Rendered as the full-screen sheet (below `lg`, or in fullscreen at any width). */
  sheetOpen: () => boolean;
  /** Either mount is showing. */
  shown: () => boolean;
  liveWidth: () => number;
  onResize: (dx: number) => void;
  onResizeEnd: () => void;
  /** Items minted after the seen-through pointer — the badge on the eye toggle. */
  unseenCount: () => number;
  toggle: () => void;
  /** Close the sheet: drop fullscreen and give focus back to the trigger. */
  closeSheet: () => void;
  selectView: (key: string) => void;
  openViewTo: (key: string) => void;
  requestTab: (tab: "preview" | "code") => void;
  pinPrev: () => void;
  pinNext: () => void;
  toggleKeeper: (item: ViewItem) => void;
  /** Whether focus is inside the panel — the gate on its scoped key bindings. */
  hasFocus: () => boolean;
  panelRef: (el: HTMLDivElement) => void;
  focusPanel: () => void;
  /** Give the caret back to the control that opens the panel. What a close does once
   *  there is no panel left to focus — the same restore `closeSheet` performs. */
  focusTrigger: () => void;
  /** For the row the conversation and the pane share their width in. */
  rowRef: (el: HTMLDivElement) => void;
  /** For the header control that opens the pane, so closing can restore focus. */
  triggerRef: (el: HTMLButtonElement) => void;
}

export function useChatViewport(
  currentId: () => string | null,
  source: ViewportSource,
): ChatViewport {
  const conversationKey = () => currentId() ?? "new";
  const { state, patch } = useViewportPersistence(conversationKey);

  const viewState = (): ViewSurfaceState =>
    state().surfaces.view ?? DEFAULT_VIEW_SURFACE;
  const patchView = (next: Partial<ViewSurfaceState>): void =>
    patch({
      surfaces: { ...state().surfaces, view: { ...viewState(), ...next } },
    });

  // The conversation's View, derived from this thread's transcript blocks
  // (presentation-only, so it's automatically thread-scoped).
  const items = createMemo(() =>
    collectViewItems(source.messages, source.snapshots()),
  );
  // What the thread has run, derived from the same transcript for the same reason —
  // the terminal blocks are already the record, and a second source would be a second
  // answer to what happened.
  const commands = createMemo(() => collectCommands(source.messages));
  const sourceInventory = createMemo(() =>
    buildInventory(collectSources(source.messages)),
  );
  const sources = createSurfaceSources({
    viewItems: items,
    tasks: source.tasks,
    plan: source.plan,
    branch: source.branch,
    subagents: source.subagents,
    commands,
    sources: sourceInventory,
  });
  const available = (id: SurfaceId): boolean => sources[id].available();
  const firstAvailable = (): SurfaceId | undefined =>
    SURFACE_IDS.find(available);
  /** What an empty panel opens onto: the first available surface that is a **panel**.
   *
   *  Not `firstAvailable` — the registry opens on `tasks`, which is a strip, so the
   *  panel region would be left empty and the operator would toggle the viewport open
   *  to find a thin bar of task rows and nothing under it. A thread with only strips
   *  falls back to the strip, which is then genuinely all there is to show. */
  const firstToOpen = (): SurfaceId | undefined =>
    SURFACE_IDS.find((id) => available(id) && shapeOf(id) === "panel") ??
    firstAvailable();

  // The pane only makes sense with something to show. Gating the effective open state
  // on that keeps a persisted-open thread that has since lost its content (or a fresh
  // chat that never had any) from showing an empty panel.
  const hasContent = () => SURFACE_IDS.some(available);

  /**
   * **What is actually on screen** — the stored arrangement with every surface that has
   * nothing in it taken out, and the accessor everything that renders or measures reads.
   *
   * `hasContent` above only asks whether *some* surface has something, so it passed while
   * the layout named a different one: a plan-mode thread with no versions opened onto a
   * View pane with nothing in it, and the plan it was waiting on an answer about was
   * behind a header button. The prune happens on read and is never patched back, so a
   * surface that is empty for a moment keeps its place in the arrangement — see
   * `retainAvailable`, and `clampWidth`, which reads the same way for the same reason.
   */
  const liveLayout = createMemo<ViewportLayout | null>(() => {
    const stored = state().layout;
    if (stored === null) return null;
    const next = retainAvailable(stored, available);
    return isEmpty(next) ? null : next;
  });
  const shown = () => liveLayout() !== null;

  /** Put `id` on screen, remembering the arrangement as the restore point.
   *
   *  Tiled, like a header click: a surface that arrives on its own — a new version,
   *  a plan waiting on a yes — goes *beside* what is open. Opening it without the
   *  tiling context would hand the whole panel region to it and take down whatever
   *  the operator was reading, which is the opposite of what arriving means.
   *
   *  **`focus` is a parameter because two surfaces can arrive in the same pass.** They
   *  both belong on screen; only one of them can be the one the operator is looking at,
   *  and taking focus unconditionally means the *last* to arrive wins it — which is how
   *  a plan waiting on a yes ended up behind an empty View. The caller decides; see the
   *  arrival effect below. */
  const show = (id: SurfaceId, opts?: { focus?: boolean }): void => {
    const focus = opts?.focus ?? true;
    const current = state().layout ?? emptyLayout();
    // Already in the layout: nothing to open, and nothing to move. *Showing* is about
    // putting a surface on screen; a surface that is already there and is not where the
    // operator is looking is a surface they left, and pulling them back to it on an
    // arrival is the milder version of the bug `arrivalClaims` exists to stop. A caller
    // that does mean "put this in front of me" says so by revealing as well — see
    // `showSurface`.
    if (hasSurface(current, id)) return;
    const next = openSurface(current, id, tiling());
    widenFor(panelSurfacesOf(retainAvailable(next, available)));
    patch({
      layout: next,
      lastLayout: next,
      ...(focus ? { focused: id } : {}),
    });
  };
  /** Reopen onto what the operator was last looking at — or, where that is nothing they
   *  could look at now, onto the first surface that has something.
   *
   *  It used to fall back to a hardcoded `leaf("view")`, which is how a thread whose only
   *  content was a plan opened onto an empty View pane. The registry's order is already
   *  declared by how much a surface can be waiting on the operator, so its first available
   *  *panel* is the right answer — see `firstToOpen` for why the strip at the head of that
   *  order is not. */
  const open = () => {
    // The *visible* layout, so a stored arrangement whose every surface has since emptied
    // counts as shut — which is what the operator is looking at, and what `toggle` means.
    if (liveLayout() !== null) return;
    const remembered = state().lastLayout;
    const first = firstToOpen();
    // Built through `openSurface` rather than as a literal, so the fallback files the
    // surface by its own shape — the registry's first available entry is often `tasks`,
    // a strip, and a strip dropped into the panel slot renders a document in a row of a
    // list (the reshape `pruneLayout` exists to catch).
    const restore =
      remembered !== null && !isEmpty(retainAvailable(remembered, available))
        ? remembered
        : first !== undefined
          ? openSurface(emptyLayout(), first, tiling())
          : null;
    if (restore === null) return;
    patch({
      layout: restore,
      focused: surfacesOf(retainAvailable(restore, available))[0] ?? null,
    });
  };
  const close = () => {
    const current = state().layout;
    if (current === null) return;
    patch({ layout: null, lastLayout: current });
  };
  const toggle = () => (shown() ? close() : open());

  /** What the splitting policy divides, and how far it may divide it. Three panes
   *  is generous at full screen and too many beside a transcript, so the cap is the
   *  presentation's rather than a constant. */
  const tiling = (): TilingContext => ({
    box: panelBox(),
    cap: state().fullscreen ? 3 : 2,
  });

  const isOpen = (id: SurfaceId): boolean => {
    const layout = liveLayout();
    return layout !== null && hasSurface(layout, id);
  };
  /** A header button's click. Closing the last surface closes the panel rather
   *  than leaving an empty frame behind — "open with nothing in it" is a state
   *  the layout deliberately cannot express. */
  const toggleSurface = (id: SurfaceId): void => {
    const next = toggleLayout(state().layout ?? emptyLayout(), id, tiling());
    // Emptiness is judged on what is *visible*: a stored layout can still name a surface
    // with nothing in it (`retainAvailable`), and closing the last one the operator can
    // actually see is closing the panel whether or not that remnant is in the record.
    if (isEmpty(retainAvailable(next, available))) {
      close();
      return;
    }
    // A surface arriving into a panel too narrow for it widens the panel once. Only
    // on arrival, and only upward: a surface opened and closed repeatedly must not
    // ratchet the panel wider each time.
    widenFor(panelSurfacesOf(retainAvailable(next, available)));
    // Which surface is now current. Opening one makes it current; *closing* one must
    // not, and it used to — `focused` was left pointing at the pane that had just gone,
    // so the full-screen sheet titled itself after it and the surface-scoped bindings
    // gated on it. On a close the operator's own pane keeps the caret whenever it
    // survives: closing something they were not working in is not a reason to move
    // them, and `surfacesOf` lists strips first, so falling back unconditionally would
    // hand a stack of panels to the task strip above them.
    const live = retainAvailable(next, available);
    const current = state().focused;
    const focused = hasSurface(live, id)
      ? id
      : current !== null && hasSurface(live, current)
        ? current
        : (surfacesOf(live)[0] ?? null);
    patch({ layout: next, lastLayout: next, focused });
  };

  // Which surface the operator is in. Persisted rather than held in a signal of its
  // own: it is part of the arrangement, and a thread returned to should come back
  // with the same pane current as when it was left.
  //
  // Read through the *visible* layout: a stored `focused` can name a surface that has
  // since emptied, and every consumer of this — the sheet's title, the surface-scoped
  // key bindings, the pane marked current — would then be pointing at a pane nobody can
  // see. Falling back to the first visible surface keeps all three answerable.
  const focusedSurface = (): SurfaceId | null => {
    const layout = liveLayout();
    if (layout === null) return null;
    const current = state().focused;
    if (current !== null && hasSurface(layout, current)) return current;
    return surfacesOf(layout)[0] ?? null;
  };
  const setFocusedSurface = (id: SurfaceId): void => {
    if (state().focused !== id) patch({ focused: id });
  };

  /** Bring a surface to the front of the stack it shares, and focus it. */
  const revealSurface = (id: SurfaceId): void => {
    const layout = state().layout;
    if (layout === null) return;
    patch({ layout: focusInStack(layout, id), focused: id });
  };

  /** Drag a split's divider. `path` addresses which split; `ratio` is `a`'s share.
   *
   *  Applied to the **visible** layout and written back as the stored one — the only
   *  place the prune is committed rather than read through. It has to be: the path comes
   *  from the tree the host rendered, so addressing the stored tree instead would resize
   *  a different split whenever the two differ. An operator dragging a divider is
   *  rearranging what they can see, which is a fair moment to let the remembered place of
   *  a surface that has nothing in it go. */
  const adjustSplit = (path: readonly ("a" | "b")[], ratio: number): void => {
    const layout = liveLayout();
    if (layout === null) return;
    const next = resizeSplit(layout, path, ratio);
    patch({ layout: next, lastLayout: next });
  };

  /** Close one surface from its own chrome, rather than from the header. */
  const closeSurface = (id: SurfaceId): void => {
    if (isOpen(id)) toggleSurface(id);
  };

  // The aside's width, and the drag that changes it (see `panelResize.ts` for why the
  // live width is an override rather than a seeded copy).
  const { liveWidth, onResize, onResizeEnd } = createPanelResize(() => {
    // What is on screen, not what is recorded: a surface with nothing in it must not set
    // a floor on the width of a panel it is not being drawn into.
    const layout = liveLayout();
    return layout === null ? [] : panelSurfacesOf(layout);
  });

  // The newest version's key. Following it (pinnedKey null) means freshly-minted
  // versions keep advancing the view instead of leaving it stranded on a stale pick.
  const latestViewKey = (): string | null =>
    items().find((i) => i.isLatest)?.key ?? null;
  const resolvedViewKey = (): string | null =>
    viewState().pinnedKey ?? latestViewKey();
  const requestPin = (key: string | null) => patchView({ pinnedKey: key });
  const requestTab = (tab: "preview" | "code") => patchView({ activeTab: tab });
  // Pin a version — except picking the current latest clears the pin, so the pane
  // resumes following new versions as the agent mints them.
  const selectView = (key: string) =>
    requestPin(key === latestViewKey() ? null : key);
  const openViewTo = (key: string) => {
    selectView(key);
    // `show` opens the panel on its own when the layout is closed — there is no
    // arrangement without one.
    show("view");
  };

  // **Arrival.** A surface that has just gained something may put itself on screen,
  // and whether it may is the surface's own answer rather than a rule the panel
  // applies uniformly — see `surfaceSources.ts`. The claim is one-shot per
  // conversation per surface per *key*, so a manual close is respected, a surface
  // re-rendering does not reopen itself, and a genuinely new arrival (a revised plan
  // still awaiting a yes) earns a fresh one.
  //
  // Which of them the operator lands on is `arrivalClaims`' answer, not this loop's —
  // see there for why the last to arrive must not be the one that wins.
  createEffect(() => {
    const conversation = currentId();
    if (conversation === null) return;
    for (const claim of arrivalClaims(sources, (key) =>
      claimAutoOpen(`${conversation}:${key}`),
    ))
      show(claim.id, { focus: claim.focus });
  });

  // Items minted after the "seen through" pointer. Counting from a key's *position* —
  // not a raw count — means a rewind that shrinks the list and a later regrow past the
  // old count can't coincidentally read as "seen"; a dropped key (rewound away)
  // resolves to index -1, i.e. nothing seen.
  const unseenCount = () => {
    const list = items();
    const idx = list.findIndex((i) => i.key === state().seen.view);
    return Math.max(0, list.length - (idx + 1));
  };
  // Cleared whenever the panel is visible and following the latest — a pinned older
  // version leaves later unseen items counted until the operator returns to latest.
  createEffect(() => {
    if (shown() && viewState().pinnedKey === null) {
      const latest = items().at(-1)?.key ?? null;
      if (latest !== null && state().seen.view !== latest)
        patch({ seen: { ...state().seen, view: latest } });
    }
  });

  // How much width the conversation and the pane have to share — measured off the row
  // itself rather than the window (see `panelResize.ts`).
  let rowEl: HTMLDivElement | undefined;
  onMount(() => {
    if (rowEl) observeAvailableWidth(rowEl);
  });

  // The pane renders in a desktop-only aside above `lg`; below it (or in fullscreen at
  // any width) it renders in a full-screen sheet instead.
  const [isDesktop, setIsDesktop] = createSignal(true);
  onMount(() => {
    const mq = window.matchMedia("(min-width: 64rem)");
    const update = () => setIsDesktop(mq.matches);
    update();
    mq.addEventListener("change", update);
    onCleanup(() => mq.removeEventListener("change", update));
  });
  const sheetOpen = () => shown() && (state().fullscreen || !isDesktop());
  const asideOpen = () => shown() && !sheetOpen();

  let trigger: HTMLButtonElement | undefined;
  const closeSheet = () => {
    // On desktop the sheet is fullscreen, so leaving it drops back to the aside; on a
    // narrow window the sheet *is* the panel, so leaving it closes the panel. One
    // patch either way, since the two settle together.
    const current = state().layout;
    if (isDesktop()) patch({ fullscreen: false });
    else
      patch({
        fullscreen: false,
        layout: null,
        lastLayout: current ?? state().lastLayout,
      });
    trigger?.focus();
  };

  const [panelEl, setPanelEl] = createSignal<HTMLDivElement>();
  const hasFocus = () => {
    const el = panelEl();
    return el !== undefined && el.contains(document.activeElement);
  };

  /**
   * Enter or leave full screen.
   *
   * The flip is one class on the panel's wrapper — `display: contents` in the row,
   * a fixed box over the page — which is what keeps the panel from remounting. It
   * does re-attach the subtree to the *layout* tree, and a browser drops focus when
   * that happens to the focused element's ancestor. Nothing is lost but the caret,
   * and the caret is the whole control: the operator who pressed `f` would find
   * that `f`, Escape and `shift+w` had all stopped answering.
   *
   * So whatever held focus is given it back — the element itself, which survives
   * the flip, or the panel when it did not. Only if focus was in the panel to
   * begin with, and only once it has actually gone, so a toggle can never steal
   * the caret from the composer. Twice, because the loss lands during the layout
   * after this call on the way in and after the frame on the way out, and putting
   * focus back where it already is costs nothing.
   */
  const toggleFullscreen = (): void => {
    const held = hasFocus()
      ? (document.activeElement as HTMLElement | null)
      : null;
    patch({ fullscreen: !state().fullscreen });
    if (held === null) return;
    const restore = (): void => {
      if (hasFocus()) return;
      (held.isConnected ? held : panelEl())?.focus();
    };
    restore();
    setTimeout(restore);
  };

  const pinPrev = () => {
    const list = items();
    if (list.length === 0) return;
    const idx = list.findIndex((i) => i.key === resolvedViewKey());
    // A missing key (a persisted pin whose version no longer exists) follows latest,
    // same as pinNext's fallback — so "previous" steps back from the newest item
    // instead of jumping to the oldest.
    const effIdx = idx === -1 ? list.length - 1 : idx;
    const target = list[Math.max(0, effIdx - 1)];
    if (target) selectView(target.key);
  };
  const pinNext = () => {
    const list = items();
    if (list.length === 0) return;
    const idx = list.findIndex((i) => i.key === resolvedViewKey());
    if (idx === -1 || idx >= list.length - 1) requestPin(null);
    else selectView(list[idx + 1].key);
  };

  // Flip the shown snapshot's keeper bookmark. Relays to the backend; the stream store
  // applies the optimistic update and reverts on failure.
  const toggleKeeper = (item: ViewItem) => {
    if (item.snapshot)
      void source.toggleSnapshotKeeper(item.snapshot.snapshotId, !item.keeper);
  };

  return {
    state,
    patch,
    viewState,
    patchView,
    items,
    tasks: source.tasks,
    plan: source.plan,
    branch: source.branch,
    refetchBranch: source.refetchBranch,
    subagents: source.subagents,
    refetchSubagents: source.refetchSubagents,
    commands,
    // `sourceInventory`, not the `sources` above it: that one is the per-surface
    // availability record this module builds, and the two are different things that
    // one short word would happily confuse.
    sources: sourceInventory,
    available,
    firstAvailable,
    layout: liveLayout,
    isOpen,
    // Open it if it is shut, then bring it forward — the two halves of what a control
    // elsewhere in the room means by "show me this". `show` alone would leave an
    // already-open surface sitting behind its stack-mate, which is exactly the case the
    // button exists for: a plan panel the operator opened, read, and tabbed away from.
    showSurface: (id: SurfaceId) => {
      show(id);
      revealSurface(id);
    },
    toggleSurface,
    revealSurface,
    closeSurface,
    adjustSplit,
    focusedSurface,
    setFocusedSurface,
    toggleFullscreen,
    hasContent,
    asideOpen,
    sheetOpen,
    shown,
    liveWidth,
    onResize,
    onResizeEnd,
    unseenCount,
    toggle,
    closeSheet,
    selectView,
    openViewTo,
    requestTab,
    pinPrev,
    pinNext,
    toggleKeeper,
    hasFocus,
    panelRef: setPanelEl,
    focusPanel: () => panelEl()?.focus(),
    focusTrigger: () => trigger?.focus(),
    rowRef: (el) => {
      rowEl = el;
    },
    triggerRef: (el) => {
      trigger = el;
    },
  };
}
