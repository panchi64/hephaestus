import { Show, type JSX } from "solid-js";
import {
  Button,
  Icon,
  Menu,
  REVEAL_ON_GROUP_HOVER,
  Text,
  copyToClipboard,
  type MenuItem,
} from "~/ui";
import type { ChatMessage } from "../model";
import {
  answerText,
  assembleTranscript,
  hasReasoning,
  reasoningText,
} from "../blocks";
import { TURN_LOCK_HINT, useTurnLocked } from "../turnLock";

/** The one hover/focus reveal in a turn: metadata and actions surface together on
 *  the same gesture. The mechanics live in `~/ui`'s `REVEAL_ON_GROUP_HOVER`,
 *  which four surfaces now share — the turn wrapper is the unnamed `group` this
 *  hangs off. Re-exported under the old name so the turn's own model/time line
 *  keeps reading as "this turn's reveal" at its call site.
 *
 *  It matters that this is not merely `opacity-0`: on a touch device there is no
 *  hover state, so an unconditional reveal deletes the control outright — and one
 *  of the things behind this menu is Expand all, the only way to open a turn's
 *  layers at once. */
export const TURN_REVEAL_CLASS = REVEAL_ON_GROUP_HOVER;

/** Hover/focus-revealed action row for a chat turn: COPY, and everything else
 *  behind one overflow menu.
 *
 *  Seven labelled buttons per turn made every exchange read as a toolbar with a
 *  message attached, and the transcript is the thing being read. COPY stays out
 *  because it is the action reached most often and costs nothing to leave in
 *  reach; the rest — edit, regenerate, rewind, pin, delete — are deliberate acts
 *  that survive one click of indirection. */
export function MessageActions(props: {
  message: ChatMessage;
  /** Re-answer an assistant turn with the current model selection. */
  onRegenerate?: () => void;
  /** Enter edit-in-place on a user turn. */
  onEdit?: () => void;
  /** Rewind the thread to (and including) this turn. */
  onRewind?: () => void;
  /** Open a new conversation carrying history up to this turn. */
  onFork?: () => void;
  /** Delete this turn and everything after it. */
  onDelete?: () => void;
  /** Pin/unpin this turn (backend-owned flag). */
  onTogglePin?: () => void;
  /** Turn-specific entries appended to the overflow menu (e.g. expand-all). */
  extraItems?: MenuItem[];
}): JSX.Element {
  const m = () => props.message;
  const isAssistant = () => m().role === "assistant";
  // Every act that rewrites the recorded thread waits for the turn to be stopped.
  const locked = useTurnLocked();
  const lock = () => ({ disabled: locked(), hint: TURN_LOCK_HINT });

  const overflowItems = (): MenuItem[] => [
    // Lead with the turn's primary act: edit for the operator's own message,
    // regenerate/rewind for the assistant's answer.
    ...(!isAssistant() && props.onEdit
      ? [
          {
            label: "Edit",
            icon: "pen",
            ...lock(),
            onSelect: () => props.onEdit?.(),
          } satisfies MenuItem,
        ]
      : []),
    ...(isAssistant() && props.onRegenerate
      ? [
          {
            label: "Regenerate",
            icon: "refresh",
            ...lock(),
            onSelect: () => props.onRegenerate?.(),
          } satisfies MenuItem,
        ]
      : []),
    ...(isAssistant() && props.onRewind
      ? [
          {
            label: "Rewind to here",
            icon: "chevron-up",
            ...lock(),
            onSelect: () => props.onRewind?.(),
          } satisfies MenuItem,
        ]
      : []),
    // Distinct from REWIND, which moves this thread's tip: a fork leaves this
    // conversation exactly as it is and opens a second one carrying the history
    // up to here, so a promising tangent doesn't cost the thread it came from.
    ...(props.onFork
      ? [
          {
            label: "Fork from here",
            icon: "branch",
            ...lock(),
            onSelect: () => props.onFork?.(),
          } satisfies MenuItem,
        ]
      : []),
    {
      label: m().pinned ? "Unpin" : "PIN",
      icon: "pin",
      onSelect: () => props.onTogglePin?.(),
    },
    ...(props.extraItems ?? []),
    ...(props.onDelete
      ? [
          {
            label: "Delete",
            icon: "trash",
            danger: true,
            ...lock(),
            onSelect: () => props.onDelete?.(),
          } satisfies MenuItem,
        ]
      : []),
  ];

  return (
    <div class={`flex items-center gap-1 ${TURN_REVEAL_CLASS}`}>
      {/* Copy: one button on a user turn, a small menu on an assistant turn,
          where "the answer", "the whole turn", and "the reasoning" are different
          things to put on the clipboard. */}
      <Show
        when={isAssistant()}
        fallback={
          <Button
            variant="ghost"
            size="sm"
            leading="copy"
            aria-label="Copy message"
            onClick={() => copyToClipboard(m().content, "Answer")}
          >
            Copy
          </Button>
        }
      >
        <Menu
          align="left"
          trigger={
            <span
              class="inline-flex h-6 items-center gap-1 border border-transparent px-2 text-dim transition-colors hover:bg-raised hover:text-bright"
              aria-label="Copy message"
            >
              <Icon name="copy" size={12} />
              <Text variant="label" tone="dim">
                Copy
              </Text>
            </span>
          }
          items={
            [
              {
                label: "Copy answer",
                icon: "copy",
                onSelect: () =>
                  copyToClipboard(answerText(m().blocks), "Answer"),
              },
              {
                label: "Copy message",
                icon: "layers",
                onSelect: () =>
                  copyToClipboard(assembleTranscript(m().blocks), "Message"),
              },
              ...(hasReasoning(m().blocks)
                ? [
                    {
                      label: "Copy reasoning",
                      icon: "note",
                      onSelect: () =>
                        copyToClipboard(reasoningText(m().blocks), "Reasoning"),
                    } satisfies MenuItem,
                  ]
                : []),
            ] satisfies MenuItem[]
          }
        />
      </Show>

      <Menu
        align="left"
        trigger={
          <Button variant="ghost" size="sm" aria-label="Turn actions">
            ···
          </Button>
        }
        items={overflowItems()}
      />
    </div>
  );
}
