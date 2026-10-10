import {
  For,
  Match,
  Show,
  Switch,
  createSignal,
  onCleanup,
  onMount,
  type JSX,
} from "solid-js";
import {
  Button,
  Chip,
  Icon,
  Markdown,
  ScrollFade,
  Stack,
  Text,
  Textarea,
  cx,
} from "~/ui";
import { hostLabel, longTimestamp } from "~/lib/format";
import { CONTEXT_OVERFLOW_DETAIL } from "~/lib/stream";
import type { ApprovalDecision, ChatMessage, Citation } from "../model";
import { hasLayers as turnHasLayers } from "../blocks";
import { createQueuedEdit } from "../queuedEdit";
import type { ViewItem } from "../viewport/viewItems";
import { TURN_GAP } from "./BlockRow";
import { CompactionTurn } from "./CompactionTurn";
import { SubagentReport } from "./SubagentReport";
import { MessageActions, TURN_REVEAL_CLASS } from "./MessageActions";
import { MessageAttachments } from "./MessageAttachments";
import { MessageFileRefs } from "./MessageFileRefs";
import { TurnBlocks } from "./TurnBlocks";
import { TurnProgressRail } from "./TurnProgressRail";

export interface MessageItemProps {
  message: ChatMessage;
  /** Decide a turn's pending host-command approvals (terminal blocks). */
  onResolveHostCommands?: (
    messageId: string,
    decisions: ApprovalDecision[],
  ) => void | Promise<void>;
  /** Re-answer this assistant turn with the current model selection. */
  onRegenerate?: () => void;
  /** Re-ask an edited user turn as a new version. */
  onEditMessage?: (id: string, text: string) => void;
  /** Delete this turn and everything after it. */
  onDelete?: () => void;
  /** Rewind the thread to (and including) this turn. */
  onRewind?: () => void;
  /** Open a new conversation carrying history up to this turn. */
  onFork?: () => void;
  /** Switch this turn to a sibling version (branch). */
  onSwitchVersion?: (id: string, index: number) => void;
  /** Pin/unpin this turn (backend-owned flag). */
  onTogglePin?: () => void;
  /** Withdraw this still-queued steering message before the run consumes it. */
  onWithdraw?: () => void;
  /** Rewrite this still-queued steering message in place (it keeps its spot in
   *  the queue) — the queued counterpart of `onEditMessage`. */
  onEditQueued?: (text: string) => void;
  /** Hold this queued message back while its editor is open — see `UserTurn`. */
  onHoldQueued?: (held: boolean) => void;
  /** Open a View item (a version or the live head) in the side viewport, by key. */
  onOpenInView?: (key: string) => void;
  /** Re-attach to this turn's run after its transport detached (reconnect
   *  budget exhausted) — the run may still be alive server-side. */
  onReattach?: () => void;
  /** Resume a turn a bound stopped by sending a fresh "Continue." turn on the
   *  same conversation. Shown next to the persistent "Stopped:" marker. */
  onContinue?: () => void;
  /** Fold the thread and then resume it — offered beside Continue on the one stop
   *  Continue cannot clear, a request the provider refused as too large. */
  onCompactAndRetry?: () => void;
  /** A fold is already running on this thread. The room owns the flag — a turn cannot
   *  know it — and it has to reach the button, because a second press is not a harmless
   *  no-op: the fold declines (the thread is claimed) and the continue fires anyway,
   *  re-sending the same oversized request that stopped the turn in the first place. */
  compacting?: boolean;
  /** The conversation's consolidated View list — read-only here, so inline
   *  transcript chips (`TurnBlocks`) can show the same version/time/NEW
   *  metadata `ViewTimelineRail` shows for the same item. */
  viewItems?: () => ViewItem[];
  /** The key of the newest View item the operator has seen — items after it in
   *  `viewItems()` render their inline chip tagged NEW. */
  seenKey?: () => string | null;
  /** This turn sits above the newest compaction divider: still in the operator's
   *  transcript, no longer in what the model replays. Rendered as a dim pass over
   *  the whole turn — the divider says it in words, this says it at a glance.
   *  Derived by the transcript (presentation only); a turn can't know it alone. */
  dimmed?: boolean;
}

/** A single chat turn. User turns fill the row with a distinct `surface`
 *  background and right-aligned content; assistant turns sit on the base
 *  background with reasoning, tool calls, then a markdown-formatted answer and a
 *  streaming caret while in flight. A compaction entry is neither — it is a
 *  full-width divider marking where the thread's earlier turns were folded into a
 *  summary, with no actions and no bubble. */
export function MessageItem(props: MessageItemProps): JSX.Element {
  return (
    // Two nodes, deliberately: `ody-message-in` runs with `animation-fill-mode:
    // both`, and an animated opacity outranks a utility class in the cascade —
    // on one element the entry animation would silently cancel `dimmed`. The
    // outer node owns the dim state, the inner one owns the movement.
    // `data-message-id` is how the transcript finds a turn to scroll to. A DOM lookup
    // rather than a ref because the turns are rendered through a `For`, and the one
    // thing that wants to address a specific turn does so rarely and from outside it.
    <div
      data-message-id={props.message.id}
      class={cx(props.dimmed && "opacity-50")}
    >
      {/* Fires once per mounted turn, so a message glides up as it arrives and a
          streaming turn never re-animates mid-delta (its root element is not
          recreated). Opening a thread mounts its turns together, so the
          transcript settles in as one movement, not a staggered cascade. */}
      <div class="ody-message-in">
        <Switch
          fallback={
            <AssistantTurn
              message={props.message}
              onResolveHostCommands={props.onResolveHostCommands}
              onRegenerate={props.onRegenerate}
              onDelete={props.onDelete}
              onRewind={props.onRewind}
              onFork={props.onFork}
              onSwitchVersion={props.onSwitchVersion}
              onTogglePin={props.onTogglePin}
              onOpenInView={props.onOpenInView}
              onReattach={props.onReattach}
              onContinue={props.onContinue}
              onCompactAndRetry={props.onCompactAndRetry}
              compacting={props.compacting}
              viewItems={props.viewItems}
              seenKey={props.seenKey}
            />
          }
        >
          <Match when={props.message.role === "compaction"}>
            <CompactionTurn message={props.message} />
          </Match>
          <Match when={props.message.role === "subagent"}>
            <SubagentReport message={props.message} />
          </Match>
          <Match when={props.message.role === "user"}>
            <UserTurn
              message={props.message}
              onEditMessage={props.onEditMessage}
              onDelete={props.onDelete}
              onFork={props.onFork}
              onSwitchVersion={props.onSwitchVersion}
              onTogglePin={props.onTogglePin}
              onWithdraw={props.onWithdraw}
              onEditQueued={props.onEditQueued}
              onHoldQueued={props.onHoldQueued}
              onContinue={props.onContinue}
              onCompactAndRetry={props.onCompactAndRetry}
              compacting={props.compacting}
            />
          </Match>
        </Switch>
      </div>
    </div>
  );
}

/** Compact `‹ n/total ›` cycler shown when a turn has sibling versions. */
function VersionCycler(props: {
  message: ChatMessage;
  onSwitchVersion?: (id: string, index: number) => void;
}): JSX.Element {
  const count = () => props.message.versionCount ?? 1;
  const index = () => props.message.versionIndex ?? 0;
  const go = (next: number) => {
    const clamped = Math.max(0, Math.min(count() - 1, next));
    if (clamped !== index()) props.onSwitchVersion?.(props.message.id, clamped);
  };
  return (
    <Show when={count() > 1}>
      {/* `ody-fade-in` rather than a `Reveal` wrapper: this sits in a row of
          inline controls, and a block wrapper would change how it lays out. The
          fade matters because version metadata arrives a round-trip *after* the
          answer settles — the cycler would otherwise pop into a header the
          operator has already started reading. On a turn that mounts with
          siblings it just runs alongside the turn's own entry. */}
      <span class="ody-fade-in flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          leading="chevron-left"
          aria-label="Previous version"
          disabled={index() <= 0}
          onClick={() => go(index() - 1)}
        />
        <Text variant="micro" tone="dim">
          {index() + 1}/{count()}
        </Text>
        <Button
          variant="ghost"
          size="sm"
          leading="chevron-right"
          aria-label="Next version"
          disabled={index() >= count() - 1}
          onClick={() => go(index() + 1)}
        />
      </span>
    </Show>
  );
}

/** Small marker shown in a turn header when the operator has pinned it. */
function PinMarker(props: { message: ChatMessage }): JSX.Element {
  return (
    <Show when={props.message.pinned}>
      <span class="text-nominal" aria-label="Pinned" title="Pinned">
        <Icon name="pin" size={12} />
      </span>
    </Show>
  );
}

/** The persistent "Stopped:" marker for a turn a bound stopped (a time/cancel
 *  bound, a usage/context/loop limit), with a "Continue." button that resumes it by
 *  sending a fresh "Continue." turn. Shown on whichever message carries the turn's
 *  `blocked_reason` — the assistant's answer when one was produced, or the operator's
 *  own message when the bound tripped before any response landed.
 *
 *  **One stop has a remedy of its own, and it is the one Continue cannot fix.** A turn
 *  the provider refused as too large will be refused again the moment it is resumed, so
 *  Continue alone would offer the operator a button that reliably does nothing. Compact
 *  and retry folds the thread first and then continues — the same pair the chassis
 *  attempts by itself, offered by hand for the case where it could not (compaction off
 *  for the thread, no background model, a fold that freed nothing).
 *
 *  Keyed on the exact detail string the backend exports, which is why the constant is
 *  mirrored rather than matched loosely: the same string arrives live on `run.ended` and
 *  is read back from `blocked_reason` on a cold load, so the control is in the same
 *  place before and after a reload. Anything else keys nothing and reads as a plain
 *  bound. */
function BlockedFooter(props: {
  detail: string | undefined;
  onContinue?: () => void;
  onCompactAndRetry?: () => void;
  compacting?: boolean;
}): JSX.Element {
  const overflowed = () => props.detail === CONTEXT_OVERFLOW_DETAIL;
  return (
    <div class="flex flex-wrap items-center gap-2 text-warn">
      <Icon name="warning" size={12} />
      <Text variant="micro" tone="warn">
        Stopped: {props.detail ?? "a run limit was reached"}
      </Text>
      <Show when={overflowed() && props.onCompactAndRetry}>
        {/* The one control here that has to say it is working. The fold inside it holds
            a summarizer call, and the pair is not idempotent: a second press finds the
            thread already claimed, so the fold returns at once and the continue fires
            immediately — re-sending the request that was refused as too large. */}
        <Button
          variant="ghost"
          size="sm"
          leading="layers"
          pending={props.compacting}
          onClick={() => props.onCompactAndRetry?.()}
        >
          Compact and retry
        </Button>
      </Show>
      <Show when={props.onContinue}>
        <Button
          variant="ghost"
          size="sm"
          leading="play"
          onClick={() => props.onContinue?.()}
        >
          Continue
        </Button>
      </Show>
    </div>
  );
}

/** Longer than this and the turn collapses behind a SHOW MORE. Twelve lines is
 *  roughly a screenful of composer: enough that a normal question is never clipped,
 *  low enough that a pasted log or spec doesn't cost the whole scrollback. */
const USER_TURN_CLAMP_LINES = 12;

/** A user turn's text: placed right, read left.
 *
 *  The bubble stays right-aligned — that is what marks it as the operator's — but the
 *  text inside is `text-left`, because right-aligned prose with any internal structure
 *  (a list, a pasted snippet, anything with leading indentation) reads as ragged and
 *  wrong. Alignment of the block and alignment of the words are separate decisions and
 *  only the first one carries meaning here.
 *
 *  A long turn clamps to a fixed height with a fade and a SHOW MORE. Presentation-only
 *  state, deliberately not persisted: it is a way of skimming *this* scrollback right
 *  now, not a preference about the message. */
function UserText(props: { text: string }): JSX.Element {
  const [expanded, setExpanded] = createSignal(false);
  const [clampable, setClampable] = createSignal(false);
  let ref: HTMLDivElement | undefined;

  // Measured, not counted: a wrapped long line takes more rows than its newlines
  // suggest, so counting "\n" would leave a wall of text unclamped.
  //
  // **And measured again whenever the content resizes**, which a one-shot `onMount`
  // could not do once this turn became rendered markdown. `Markdown` fills the node
  // through `innerHTML` from its own effect, so at mount there is nothing inside to
  // measure: every long turn read as 0 tall, `clampable` stayed false, and the clamp
  // — along with SHOW MORE — silently never appeared. An observer also covers the
  // cases a second `onMount` would still miss: a late-loading font reflowing the
  // block, and the operator editing the turn in place.
  onMount(() => {
    if (!ref) return;
    const measure = () => {
      // Only meaningful while clamped — expanded, `clientHeight` grows to the full
      // content and the comparison would clear the flag that is drawing the control
      // the operator just used.
      if (!expanded()) setClampable(ref!.scrollHeight > ref!.clientHeight + 1);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(ref);
    onCleanup(() => ro.disconnect());
  });

  return (
    // The block around this (in `UserTurn`) is what shrinks to fit and pins right;
    // this only lays out the body and its Show more toggle inside it.
    <div class="flex flex-col items-end gap-1 px-3.5 py-2.5">
      <div
        ref={ref}
        class="relative w-full overflow-hidden"
        style={{
          "max-height": expanded()
            ? undefined
            : `calc(${USER_TURN_CLAMP_LINES} * 1.5em)`,
        }}
      >
        {/* Rendered, not printed. The operator's turn sits inches from a rendered
            answer, so a prompt carrying a list, a fence or a `>` quote showed its
            own source beside prose that had been set — the asymmetry read as the
            product taking one speaker less seriously than the other.

            This is also the *stricter* reading of §4, not a relaxation of it: the
            reading scale is `.ody-prose` only, and `variant="reading"` was the
            system's one sanctioned use of `--prose-*` outside it. Real prose
            retires that exception at identical size.

            No `streamStable` — that path exists for the answer stream's settled-
            block caching (§10.11), and the operator's turn arrives whole.

            Colour comes from `.ody-prose` rather than a `bright` override: both
            speakers then render identically, which is the point, and the sunken
            ground under the bubble is already what marks the turn as theirs (§5.1
            names it for exactly this). */}
        <Markdown class="text-left break-words text-bright!">
          {props.text}
        </Markdown>
        {/* Only while clamped, and only when there is genuinely more below. */}
        <Show when={clampable() && !expanded()}>
          <ScrollFade variant="clamp" edge="bottom" ground="raised" />
        </Show>
      </div>
      <Show when={clampable()}>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded()}
        >
          {expanded() ? "Show less" : "Show more"}
        </Button>
      </Show>
    </div>
  );
}

function UserTurn(props: {
  message: ChatMessage;
  onEditMessage?: (id: string, text: string) => void;
  onDelete?: () => void;
  onFork?: () => void;
  onSwitchVersion?: (id: string, index: number) => void;
  onTogglePin?: () => void;
  onWithdraw?: () => void;
  onEditQueued?: (text: string) => void;
  /** Ask the run to hold this queued message back while it is being edited, or to stop
   *  holding it. Absent on hosts that do not steer (a compare pane). */
  onHoldQueued?: (held: boolean) => void;
  onContinue?: () => void;
  onCompactAndRetry?: () => void;
  compacting?: boolean;
}): JSX.Element {
  const m = () => props.message;
  // The hold/draft protocol is shared with the dock's own list of queued messages —
  // see `queuedEdit.ts`. What stays here is which write the save is.
  const edit = createQueuedEdit({
    messageId: () => m().queuedMessageId,
    queued: () => m().queuedPending === true,
    initial: () => m().content,
    onHold: (held) => props.onHoldQueued?.(held),
    // Routed by the bubble's state *at save time*: a still-queued message edits in place
    // on the live run; a delivered one re-asks as a new version, so a bubble injected
    // mid-edit falls through to the normal edit path rather than 404ing on the queue.
    onSave: (text) => {
      if (m().queuedPending) props.onEditQueued?.(text);
      else props.onEditMessage?.(m().id, text);
    },
  });
  const editing = edit.editing;
  const draft = edit.draft;
  const startEdit = edit.start;
  const save = edit.save;

  return (
    // The turn is a square block pinned to the right that shrinks to its content
    // (78% cap), `raised` on the page. Its edge is drawn by borders rather than a
    // box-shadow so the geometry is exact: a 1px `line-strong` outline on the top,
    // left and bottom, and a 3px `bright` edge on the right with no grey under it.
    // No speaker label: the stamp and the actions reveal on hover beside it
    // (absolutely placed, so nothing reflows) and stay shown on touch, like every
    // other turn control.
    <div class="group flex flex-col items-end gap-1 px-4 py-3">
      <div
        class={cx(
          "relative border-y border-l border-r-[3px] border-y-line-strong border-l-line-strong border-r-bright bg-raised",
          editing() ? "w-full max-w-[78%]" : "w-fit max-w-[78%]",
        )}
        title={longTimestamp(m().createdAt)}
      >
        {/* The stamp sits outside the block, in the margin to its left and level with
            the first line: inside a shrink-to-fit block there is no room for it, and
            laid over the block it ran across the outline. The full date also rides in
            the block's title for anyone who never hovers. */}
        <time
          class={cx(
            "pointer-events-none absolute top-2.5 right-full mr-2 font-mono text-[10px] leading-none whitespace-nowrap text-dim",
            TURN_REVEAL_CLASS,
            "group-focus-within:opacity-100",
          )}
          dateTime={m().createdAt}
        >
          {longTimestamp(m().createdAt)}
        </time>
        {/* The actions sit outside the block too, in the same margin but level with
            its foot, so the stamp (top) and they never meet. */}
        <Show when={!editing() && !m().queuedPending}>
          <div class="absolute right-full bottom-0 mr-2">
            <MessageActions
              message={m()}
              onEdit={startEdit}
              onFork={props.onFork}
              onDelete={props.onDelete}
              onTogglePin={props.onTogglePin}
            />
          </div>
        </Show>
        {/* The block names no speaker: its place on the right and its edge already
            say whose it is, and a tag row reserved for a label left an empty band
            over every message. The row exists only when it has state to show. */}
        <Show
          when={m().queuedPending || m().pinned || (m().versionCount ?? 1) > 1}
        >
          <div class="relative flex items-center justify-end gap-2 px-2.5 pt-[7px]">
            <Show when={m().queuedPending && !editing()}>
              <Button
                variant="ghost"
                size="sm"
                leading="pen"
                aria-label="Edit queued message"
                onClick={startEdit}
              >
                Edit
              </Button>
              <Button
                variant="ghost"
                size="sm"
                leading="close"
                aria-label="Withdraw queued message"
                onClick={() => props.onWithdraw?.()}
              >
                Withdraw
              </Button>
            </Show>
            <Show when={m().queuedPending}>
              <Text variant="label" tone="warn">
                Queued
              </Text>
            </Show>
            {/* A queue that has stopped moving says so. The hold is what makes editing
              safe, and it is also the one state in which the run is waiting on the
              operator without having asked them for anything — silence there reads as
              the agent having stalled. */}
            <Show when={m().queuedPending && m().queuedHeld}>
              <Text variant="label" tone="dim">
                Held
              </Text>
            </Show>
            <PinMarker message={m()} />
            <VersionCycler
              message={m()}
              onSwitchVersion={props.onSwitchVersion}
            />
          </div>
        </Show>
        <Show
          when={editing()}
          fallback={
            <Show when={m().content}>
              <UserText text={m().content} />
            </Show>
          }
        >
          <div class="px-3.5 pt-1.5 pb-[11px]">
            <Textarea
              value={draft()}
              rows={3}
              onInput={(e) => edit.write(e.currentTarget.value)}
              aria-label="Edit message"
            />
            <div class="mt-1 flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={edit.cancel}>
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                disabled={!draft().trim()}
                onClick={save}
              >
                Save
              </Button>
            </div>
          </div>
        </Show>
      </div>
      <Show when={!editing()}>
        <>
          <Show when={m().attachmentIds?.length}>
            <MessageAttachments ids={m().attachmentIds!} />
          </Show>
          <Show when={m().fileRefs?.length}>
            <MessageFileRefs paths={m().fileRefs!} />
          </Show>
          {/* Sets the expectation the QUEUED badge alone doesn't: the run hands
                this to the model at its next model call, so a reply already being
                written finishes first and this lands after it — not woven into the
                text mid-sentence. Without saying so, an answer that streams on
                past the message reads as the steering having been ignored. */}
          <Show when={m().queuedPending}>
            <Text variant="micro" tone="dim" class="max-w-4/5 text-right">
              Goes to the model at its next step — after the current reply if it
              finishes first.
            </Text>
          </Show>
          {/* A bound that tripped before any response landed stamps the stop on
                this message (the turn's own prompt) — show the marker + Continue
                here, where the operator's own words sit. */}
          <Show when={m().blocked}>
            <BlockedFooter
              detail={m().blockedDetail}
              onContinue={props.onContinue}
              onCompactAndRetry={props.onCompactAndRetry}
              compacting={props.compacting}
            />
          </Show>
        </>
      </Show>
    </div>
  );
}

const SOURCES_PREVIEW_COUNT = 3;

/** The web sources a turn's `web_search`/`web_fetch` calls surfaced, as a compact
 *  row of chips beneath the answer — each opens its source in a new tab. Chips
 *  show base domains rather than full titles to stay compact, and collapse behind
 *  a toggle past SOURCES_PREVIEW_COUNT so a research-heavy turn doesn't dominate
 *  the transcript. */
function SourcesRow(props: { citations: Citation[] }): JSX.Element {
  const [expanded, setExpanded] = createSignal(false);
  const hidden = () =>
    Math.max(props.citations.length - SOURCES_PREVIEW_COUNT, 0);
  const shown = () =>
    expanded()
      ? props.citations
      : props.citations.slice(0, SOURCES_PREVIEW_COUNT);

  return (
    <div class="flex flex-wrap items-center gap-2">
      <Text variant="plate" tone="dim">
        Sources
      </Text>
      <For each={shown()}>
        {(c, i) => (
          <Chip
            // A corpus passage has a locator, not an address — there is no tab to
            // open it in, and a chip that opened `about:blank` would read as a
            // broken link rather than as a source on the operator's own disk.
            onClick={
              c.url
                ? () => window.open(c.url!, "_blank", "noopener,noreferrer")
                : undefined
            }
          >
            [{i() + 1}] {c.url ? hostLabel(c.url) : (c.ref ?? c.title ?? "")}
          </Chip>
        )}
      </For>
      <Show when={hidden() > 0}>
        <Button
          variant="ghost"
          size="sm"
          leading={expanded() ? "chevron-up" : "chevron-down"}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded() ? "Show less" : `+${hidden()} MORE`}
        </Button>
      </Show>
    </div>
  );
}

function AssistantTurn(props: {
  message: ChatMessage;
  onResolveHostCommands?: MessageItemProps["onResolveHostCommands"];
  onRegenerate?: MessageItemProps["onRegenerate"];
  onDelete?: MessageItemProps["onDelete"];
  onRewind?: MessageItemProps["onRewind"];
  onFork?: MessageItemProps["onFork"];
  onSwitchVersion?: MessageItemProps["onSwitchVersion"];
  onTogglePin?: MessageItemProps["onTogglePin"];
  onOpenInView?: MessageItemProps["onOpenInView"];
  onReattach?: MessageItemProps["onReattach"];
  onContinue?: MessageItemProps["onContinue"];
  onCompactAndRetry?: MessageItemProps["onCompactAndRetry"];
  compacting?: MessageItemProps["compacting"];
  viewItems?: MessageItemProps["viewItems"];
  seenKey?: MessageItemProps["seenKey"];
}): JSX.Element {
  const m = () => props.message;
  // Tri-state: undefined = each layer keeps its own default; true/false = force
  // every layer open/closed at once. Toggling sets the opposite of its last
  // explicit state (first press expands).
  const [forceOpen, setForceOpen] = createSignal<boolean | undefined>(
    undefined,
  );
  const hasLayers = () => turnHasLayers(m().blocks);
  const toggleAll = () => setForceOpen((v) => !v);

  return (
    // `py-3` is the gap between turns — no list gap sits between them, so this
    // padding and the operator fill's edge are the whole of it. `mb-1` keeps the
    // timestamp with the answer it stamps: its 24px row (the actions' height)
    // already centres a 10px stamp in 7px of air either side.
    <div class="group px-4 py-3">
      <div class="mb-1 flex items-center gap-2">
        {/* WHEN, on hover or focus — the row is a fixed 24px (the actions'
            height) whether or not the stamp is showing, so revealing it moves
            nothing; on touch the shared reveal keeps it shown. Written out
            rather than `5D AGO`, because a transcript is scrolled back through
            and a relative stamp turns into arithmetic for every turn above the
            one under the cursor. `micro` mono: a timestamp is emitted, not
            written (§2). The full date rides in the title as well. */}
        <span
          class={cx(TURN_REVEAL_CLASS, "group-focus-within:opacity-100")}
          title={longTimestamp(m().createdAt)}
        >
          <Text variant="micro" tone="dim">
            <time dateTime={m().createdAt}>{longTimestamp(m().createdAt)}</time>
          </Text>
        </span>
        <PinMarker message={m()} />
        <VersionCycler message={m()} onSwitchVersion={props.onSwitchVersion} />
        <span class="ml-auto">
          <MessageActions
            message={m()}
            onRegenerate={props.onRegenerate}
            onRewind={props.onRewind}
            onFork={props.onFork}
            onDelete={props.onDelete}
            onTogglePin={props.onTogglePin}
            extraItems={
              hasLayers()
                ? [
                    {
                      label: forceOpen() ? "Collapse all" : "Expand all",
                      icon: "layers",
                      onSelect: toggleAll,
                    },
                  ]
                : undefined
            }
          />
        </span>
      </div>

      <Stack gap={TURN_GAP}>
        {/* What's happening now. What it took, once settled, is the work log's
            own header — see `WorkLogHeader`. */}
        <TurnProgressRail
          blocks={m().blocks}
          streaming={m().streaming}
          queued={m().queued}
        />
        {/* The turn's ordered, interleaved blocks — the source of truth. */}
        <TurnBlocks
          blocks={m().blocks}
          streaming={m().streaming}
          forceOpen={forceOpen()}
          onResolveHostCommands={(decisions) =>
            props.onResolveHostCommands?.(m().id, decisions)
          }
          onOpenInView={props.onOpenInView}
          viewItems={props.viewItems}
          seenKey={props.seenKey}
        />
        <Show when={m().citations?.length}>
          <SourcesRow citations={m().citations!} />
        </Show>
        <Show when={m().blocked}>
          <BlockedFooter
            detail={m().blockedDetail}
            onContinue={props.onContinue}
            onCompactAndRetry={props.onCompactAndRetry}
            compacting={props.compacting}
          />
        </Show>
        <Show when={m().detached}>
          <div class="flex items-center gap-2 text-alert">
            <Icon name="warning" size={12} />
            <Text variant="micro" tone="alert">
              Connection lost — the response may still be running.
            </Text>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => props.onReattach?.()}
            >
              Reconnect
            </Button>
          </div>
        </Show>
      </Stack>
    </div>
  );
}
