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
import type {
  ApprovalDecision,
  ChatMessage,
  Citation,
  ContentPart,
} from "../model";
import { hasLayers as turnHasLayers } from "../blocks";
import { TURN_LOCK_HINT, useTurnLocked } from "../turnLock";
import type { ViewItem } from "../viewport/viewItems";
import { TURN_GAP } from "./BlockRow";
import { CompactionTurn } from "./CompactionTurn";
import { EnvelopeRow } from "./EnvelopeRow";
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
  onEditMessage?: (id: string, text: string) => void | Promise<boolean>;
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
  /** Open a View item (a version or the live head) in the side viewport, by key. */
  onOpenInView?: (key: string) => void;
  /** Re-attach to this turn's run after its transport detached; the run may still be alive server-side. */
  onReattach?: () => void;
  /** Resume a bound-stopped turn by sending a fresh "Continue." turn; shown beside the "Stopped:" marker. */
  onContinue?: () => void;
  /** Fold the thread, then resume; offered for the one stop Continue cannot clear (request refused as too large). */
  onCompactAndRetry?: () => void;
  /** A fold is already running on this thread. It must reach the button: a second press
   *  makes the fold decline (thread claimed) while the continue fires anyway, re-sending
   *  the oversized request. Owned by the room; a turn cannot know it. */
  compacting?: boolean;
  /** The consolidated View list, so inline chips (`TurnBlocks`) show the same metadata as `ViewTimelineRail`. */
  viewItems?: () => ViewItem[];
  /** Key of the newest View item seen; items after it render their inline chip tagged NEW. */
  seenKey?: () => string | null;
  /** This turn sits above the newest compaction divider: still shown, no longer replayed
   *  to the model. Derived by the transcript. */
  dimmed?: boolean;
  /** For a sub-agent report or message: the callsign of the agent that sent it. */
  subagentCallsign?: string;
}

/** A single chat turn. A compaction entry is a full-width divider with no actions or bubble. */
export function MessageItem(props: MessageItemProps): JSX.Element {
  return (
    // Two nodes: an animated opacity (`heph-message-in`, fill-mode both) outranks the
    // `dimmed` utility, so the outer node owns the dim and the inner one the movement.
    // `data-message-id` is how the transcript finds a turn to scroll to.
    <div
      data-message-id={props.message.id}
      class={cx(props.dimmed && "opacity-50")}
    >
      {/* Fires once per mounted turn, so a streaming turn never re-animates mid-delta. */}
      <div class="heph-message-in">
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
            <SubagentReport
              message={props.message}
              callsign={props.subagentCallsign}
            />
          </Match>
          <Match when={props.message.role === "notice"}>
            <EnvelopeRow
              variant="notice"
              kind={props.message.notice}
              body={props.message.content}
            />
          </Match>
          <Match when={props.message.role === "user"}>
            <UserTurn
              message={props.message}
              onEditMessage={props.onEditMessage}
              onDelete={props.onDelete}
              onFork={props.onFork}
              onSwitchVersion={props.onSwitchVersion}
              onTogglePin={props.onTogglePin}
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
  const locked = useTurnLocked();
  const go = (next: number) => {
    const clamped = Math.max(0, Math.min(count() - 1, next));
    if (clamped !== index()) props.onSwitchVersion?.(props.message.id, clamped);
  };
  return (
    <Show when={count() > 1}>
      {/* `heph-fade-in`, not a `Reveal` wrapper, which would change inline layout. Version
          metadata arrives a round-trip after the answer settles and would otherwise pop in. */}
      <span class="heph-fade-in flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          leading="chevron-left"
          aria-label="Previous version"
          title={locked() ? TURN_LOCK_HINT : undefined}
          disabled={index() <= 0 || locked()}
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
          title={locked() ? TURN_LOCK_HINT : undefined}
          disabled={index() >= count() - 1 || locked()}
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

/** The persistent "Stopped:" marker with a Continue button, shown on whichever message
 *  carries the turn's `blocked_reason` (the answer, or the operator's own message if the
 *  bound tripped before any response).
 *
 *  A turn refused as too large would be refused again on Continue, so that stop also
 *  offers Compact and retry (fold, then continue) for when the chassis's own attempt
 *  could not run (compaction off, no background model, a fold that freed nothing).
 *  Keyed on the backend's exact detail string, which arrives live on `run.ended` and
 *  on cold load from `blocked_reason`; any other detail reads as a plain bound. */
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
        {/* Must show pending: a second press finds the thread claimed, the fold returns at
            once and the continue re-sends the oversized request. */}
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

/** Longer turns collapse behind a SHOW MORE. */
const USER_TURN_CLAMP_LINES = 12;

/** A user turn's text: the bubble is right-aligned, the words `text-left` (right-aligned
 *  lists and snippets read as ragged). A long turn clamps with a fade and a SHOW MORE;
 *  that state is deliberately not persisted. */
function UserText(props: { text: string; parts?: ContentPart[] }): JSX.Element {
  const [expanded, setExpanded] = createSignal(false);
  const [clampable, setClampable] = createSignal(false);
  let ref: HTMLDivElement | undefined;

  // Measured, not counted (wrapped lines take more rows than newlines), and re-measured on
  // resize: `Markdown` fills the node via `innerHTML` from its own effect, so nothing is
  // there to measure at mount. Also covers late font reflow and in-place edits.
  onMount(() => {
    if (!ref) return;
    const measure = () => {
      // Skipped while expanded: `clientHeight` grows to the content and would clear the
      // flag drawing the control just used.
      if (!expanded()) setClampable(ref!.scrollHeight > ref!.clientHeight + 1);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(ref);
    onCleanup(() => ro.disconnect());
  });

  return (
    <div class="flex flex-col items-end gap-1 px-3.5 py-2.5">
      <div
        ref={ref}
        class="relative w-full overflow-hidden"
        classList={{ hidden: !props.text }}
        style={{
          "max-height": expanded()
            ? undefined
            : `calc(${USER_TURN_CLAMP_LINES} * 1.5em)`,
        }}
      >
        {/* Rendered markdown so both speakers read alike; the reading scale is `.heph-prose` only.
            No `streamStable`: that is for the answer stream's block caching and this turn arrives whole. */}
        <Markdown class="text-left break-words text-bright!">
          {props.text}
        </Markdown>
        <Show when={clampable() && !expanded()}>
          <ScrollFade edge="bottom" ground="raised" />
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
      {/* Text the operator did not write (attachment text, a routine's previous run) gets its own row. */}
      <For each={props.parts}>
        {(part) => (
          <div class="w-full text-left">
            <EnvelopeRow variant="part" part={part} scope="message" />
          </div>
        )}
      </For>
    </div>
  );
}

function UserTurn(props: {
  message: ChatMessage;
  onEditMessage?: MessageItemProps["onEditMessage"];
  onDelete?: () => void;
  onFork?: () => void;
  onSwitchVersion?: (id: string, index: number) => void;
  onTogglePin?: () => void;
  onContinue?: () => void;
  onCompactAndRetry?: () => void;
  compacting?: boolean;
}): JSX.Element {
  const m = () => props.message;
  const [editing, setEditing] = createSignal(false);
  const [draft, setDraft] = createSignal("");
  const startEdit = (): void => {
    setDraft(m().content);
    setEditing(true);
  };
  const cancel = (): void => {
    setEditing(false);
  };
  const save = async (): Promise<void> => {
    const text = draft().trim();
    if (!text) {
      setEditing(false);
      return;
    }
    // Stays open with the draft when the edit was not submitted.
    if ((await props.onEditMessage?.(m().id, text)) !== false)
      setEditing(false);
  };

  return (
    // No speaker label: the stamp and actions reveal on hover beside the block (absolutely
    // placed, so nothing reflows) and stay shown on touch.
    <div class="group flex flex-col items-end gap-1 px-4 py-3">
      <div
        class={cx(
          "relative border border-line-strong bg-raised",
          editing() ? "w-full max-w-[78%]" : "w-fit max-w-[78%]",
        )}
        title={longTimestamp(m().createdAt)}
      >
        {/* In the margin left of the block: a shrink-to-fit block has no room, and over it the stamp crosses the outline. */}
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
        {/* Same margin, level with the foot, so the stamp and actions never meet. */}
        <Show when={!editing()}>
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
        {/* The row exists only when it has state to show; no empty band over every message. */}
        <Show when={m().pinned || (m().versionCount ?? 1) > 1}>
          <div class="relative flex items-center justify-end gap-2 px-2.5 pt-[7px]">
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
            <Show when={m().content || m().parts?.length}>
              <UserText text={m().content} parts={m().parts} />
            </Show>
          }
        >
          <div class="px-3.5 pt-1.5 pb-[11px]">
            <Textarea
              value={draft()}
              rows={3}
              onInput={(e) => setDraft(e.currentTarget.value)}
              aria-label="Edit message"
            />
            <div class="mt-1 flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={cancel}>
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                disabled={!draft().trim()}
                onClick={() => void save()}
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
          {/* A bound that tripped before any response stamps the stop on the prompt itself. */}
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

/** Chips for the sources a turn's `web_search`/`web_fetch` calls surfaced, collapsed
 *  behind a toggle past SOURCES_PREVIEW_COUNT. */
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
            // A corpus passage has a locator, not an address; a chip opening `about:blank` reads as a broken link.
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
  // undefined = each layer keeps its own default; true/false forces every layer open/closed.
  const [forceOpen, setForceOpen] = createSignal<boolean | undefined>(
    undefined,
  );
  const hasLayers = () => turnHasLayers(m().blocks);
  const toggleAll = () => setForceOpen((v) => !v);

  return (
    // `py-3` is the whole gap between turns; no list gap sits between them.
    <div class="group px-4 py-3">
      <div class="mb-1 flex items-center gap-2">
        {/* Revealed on hover or focus; the row is a fixed 24px (the actions' height) so
            revealing moves nothing. Absolute rather than relative (`5D AGO`) because a
            scrolled-back transcript makes relative stamps arithmetic. */}
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
        {/* Live state only; once settled, the work log's header (`WorkLogHeader`) says what it took. */}
        <TurnProgressRail
          blocks={m().blocks}
          streaming={m().streaming}
          queued={m().queued}
        />
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
