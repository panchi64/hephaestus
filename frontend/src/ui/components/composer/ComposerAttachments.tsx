import { For, Show, type JSX } from "solid-js";
import { cx } from "../../cx";
import { Icon } from "../../primitives/Icon";
import { Text } from "../../primitives/Text";
import {
  HIDDEN_FILE_INPUT,
  type FileDropApi,
} from "../../primitives/useFileDrop";
import { AttachmentChip, type ComposerAttachment } from "../AttachmentChip";
import { StatusCell } from "../StatusBar";
import { Tooltip } from "../Tooltip";
import type { ComposerAttachmentsApi } from "./types";

/** The attach affordance: a button that opens the picker. Only mounted when an
 *  attachments controller is wired, so non-attachment surfaces are unchanged.
 *
 *  A plus, not a paperclip: what it means at the head of the strip is "add something
 *  to this message", and a `+` says that at a glance. Disabled rather than hidden while
 *  a run streams or a message is recalled, so the strip never reflows under the
 *  pointer. */
export function AttachButton(props: {
  attachments: ComposerAttachmentsApi | undefined;
  drop: FileDropApi;
  disabled: boolean;
}): JSX.Element {
  return (
    <Show when={props.attachments}>
      <Tooltip label="Attach files" side="top" delay={600}>
        <StatusCell
          aria-label="Attach files"
          disabled={props.disabled}
          onClick={props.drop.openPicker}
          class="px-3"
        >
          <Icon name="plus" size={14} />
        </StatusCell>
      </Tooltip>
    </Show>
  );
}

/** The picker's hidden input lives in the card, not beside the cell: the bar rules
 *  its groups' direct children, so a sibling input there would draw a hairline of its
 *  own, and inside the button its programmatic click would bubble back into it. */
export function AttachFileInput(props: {
  attachments: ComposerAttachmentsApi | undefined;
  drop: FileDropApi;
}): JSX.Element {
  return (
    <Show when={props.attachments}>
      <input
        ref={props.drop.bindInput}
        {...HIDDEN_FILE_INPUT}
        {...props.drop.inputHandlers}
      />
    </Show>
  );
}

/** Attachment chips, under the field: square mono chips, each with its type, its
 *  status, a KB-membership toggle, a remove control, and — once failed — its reason
 *  and a retry. Not over a recalled message: the chips belong to the operator's own
 *  draft, which is off screen until the recall ends, and shown here they would read
 *  as riding the edit. */
export function AttachmentChips(props: {
  attachments: ComposerAttachmentsApi | undefined;
  items: ComposerAttachment[];
  recalling: boolean;
  lg: boolean;
}): JSX.Element {
  return (
    <Show
      when={props.attachments && !props.recalling && props.items.length > 0}
    >
      <div
        class={cx("flex flex-wrap gap-1.5 pb-2.5", props.lg ? "px-4" : "px-3")}
      >
        <For each={props.items}>
          {(a) => (
            <AttachmentChip
              variant="console"
              name={a.name}
              status={a.status}
              error={a.error}
              kbExcluded={a.kbExcluded}
              onToggleKbExcluded={() =>
                props.attachments?.toggleKbExcluded(a.id)
              }
              onRetry={() => props.attachments?.retry(a.id)}
              onRemove={() => props.attachments?.remove(a.id)}
            />
          )}
        </For>
      </div>
    </Show>
  );
}

/** Why a held send stopped holding. Inline in the card, beside the chips it is
 *  about, rather than a toast: the fix — retry or remove — is right there. */
export function UploadNote(props: {
  failed: boolean;
  lg: boolean;
}): JSX.Element {
  return (
    <Show when={props.failed}>
      <div role="alert" class={cx("pb-2", props.lg ? "px-4" : "px-3")}>
        <Text variant="micro" tone="alert">
          An upload failed — retry or remove it
        </Text>
      </div>
    </Show>
  );
}

/** While files hover the unit: the console's own vocabulary — a bright ruled frame
 *  and a plate — rather than a dashed tinted zone. Square, like the unit it covers. */
export function DropOverlay(props: {
  attachments: ComposerAttachmentsApi | undefined;
  dragging: boolean;
}): JSX.Element {
  return (
    <Show when={props.attachments && props.dragging}>
      <div class="heph-composer-drop">
        <Text variant="plate" tone="bright">
          Drop to attach
        </Text>
      </div>
    </Show>
  );
}
