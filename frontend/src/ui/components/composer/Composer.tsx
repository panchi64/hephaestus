import { Show, children, createSignal, type JSX } from "solid-js";
import { cx } from "../../cx";
import { useFileDrop } from "../../primitives/useFileDrop";
import { StatusBar } from "../StatusBar";
import { ComposerActions } from "./ComposerActions";
import {
  AttachButton,
  AttachFileInput,
  AttachmentChips,
  DropOverlay,
  UploadNote,
} from "./ComposerAttachments";
import { ComposerField } from "./ComposerField";
import { ComposerHeader } from "./ComposerHeader";
import { ComposerMenu } from "./ComposerMenu";
import { createComposerKeys } from "./composerKeys";
import { createComposerMenuController } from "./composerMenuController";
import type { ComposerProps } from "./types";
import { createComposerAnnouncer } from "./useComposerAnnouncer";
import { createComposerDraft } from "./useComposerDraft";
import { createComposerSend } from "./useComposerSend";

/**
 * Message input. ⌘/Ctrl+Enter sends; Enter and Shift+Enter insert a newline, so a
 * stray Enter can never split one message into two sends. Drafts auto-save to
 * localStorage (per `storageKey`) and restore on return. The docked bar and the hero
 * field differ only by the `size` prop. With an `attachments` controller, files can be
 * attached (drop, paste or pick); a send with files still uploading is held until they land.
 */
export function Composer(props: ComposerProps): JSX.Element {
  const [text, setText] = createSignal("");
  // A recalled message while one is open, else the draft. Kept as two values so the
  // draft never needs stashing or protecting from persistence; it is just not shown.
  const recalled = () => props.recall?.draft() ?? null;
  const recalling = () => recalled() !== null;
  const value = () => recalled() ?? text();
  let field: HTMLTextAreaElement | undefined;
  const getField = () => field;
  const lg = () => props.size === "lg";

  // Menu state and keys belong to `composerMenuController`; rows and pick meaning come
  // from the feature via `props.menu`. A `/` in a recalled message is text, since the
  // edit goes back to the queue rather than through a send.
  const menuCtl = createComposerMenuController({
    menu: () => props.menu,
    field: getField,
    text,
    setText,
    suspended: recalling,
  });

  const items = () => props.attachments?.items() ?? [];

  // Drop and paste work only while the attach button would. Mid-run files ride a queued send.
  const drop = useFileDrop(
    (files) => props.attachments?.attach(files),
    () => !props.disabled && !recalling(),
  );

  const { announcement, announce } = createComposerAnnouncer(props, recalling);
  createComposerDraft(props, text, setText, getField);
  const send = createComposerSend({
    props,
    text,
    setText,
    items,
    field: getField,
    announce,
  });
  const { onKeyDown, saveRecall } = createComposerKeys({
    props,
    menuCtl,
    field: getField,
    text,
    recalling,
    recalled,
    awaiting: send.awaiting,
    submit: send.submit,
    cancelAwaiting: send.cancelAwaiting,
    announce,
  });

  const hasDraft = () => Boolean(text().trim());

  // Each slot is resolved once, like the header's.
  const controls = children(() => props.controls);
  const trailing = children(() => props.trailing);

  const dropAttrs = () => (props.attachments ? drop.dropHandlers : {});

  /* One layout and one ruled `line-strong` unit for both sizes (only padding differs):
     plate, full-width field, attachment chips, then the receded strip (`+`, pickers,
     thread state, commit keys; always drawn since Send lives there).

     The point of action is marked at rest by the opaque fill and ruled edge: no ticks,
     shadow or light, since shadows are for things that float. A live run turns the
     plate info with a blinking square. */
  // `class` is layout glue for the whole unit. `heph-framed` makes the fill glass on a
  // page with the dot field so it reads through the composer; a bare composer has no
  // fill to trade. The whole unit is the drop target.
  return (
    <div
      class={cx(
        "heph-composer",
        !props.bare && "heph-framed bg-surface",
        props.class,
      )}
      data-live={
        props.streaming && !props.waiting && !recalling() ? "" : undefined
      }
      data-queued={props.queued ? "" : undefined}
      data-bare={props.bare ? "" : undefined}
      {...dropAttrs()}
    >
      <DropOverlay
        attachments={props.attachments}
        dragging={drop.isDragging()}
      />
      {/* `FloatingPanel` portals it to the body, clear of the composer's stacking context
          and the transcript's `overflow-auto`; mounted here for the card's lifetime. */}
      <Show when={props.menu}>
        <ComposerMenu
          open={menuCtl.open()}
          groups={menuCtl.groups()}
          anchor={menuCtl.anchor}
          activeId={menuCtl.activeId()}
          loading={menuCtl.loading()}
          onPick={menuCtl.pick}
          onActivate={(item) => menuCtl.setActiveId(item.id)}
        />
      </Show>
      <AttachFileInput attachments={props.attachments} drop={drop} />
      <ComposerHeader
        recalling={recalling()}
        recallLabel={props.recall?.label}
        waiting={props.waiting}
        title={props.title}
        headerStart={props.headerStart}
        headerEnd={props.headerEnd}
        storageKey={props.storageKey}
        hasDraft={hasDraft()}
      />
      <ComposerField
        value={value()}
        tokens={Boolean(props.menu)}
        recalling={recalling()}
        lg={lg()}
        ariaLabel={
          recalling() ? props.recall?.label : (props.label ?? "Message")
        }
        placeholder={props.placeholder}
        disabled={props.disabled}
        menuOpen={menuCtl.open()}
        activeDescendant={
          menuCtl.open() ? (menuCtl.activeId() ?? undefined) : undefined
        }
        fieldRef={(el) => {
          field = el;
        }}
        onInput={(next) => {
          if (recalling()) return props.recall?.write(next);
          setText(next);
          menuCtl.sync();
        }}
        onKeyDown={onKeyDown}
        onCaret={menuCtl.sync}
        onBlur={menuCtl.dismiss}
        onPaste={props.attachments ? drop.pasteHandlers.onPaste : undefined}
      />
      <AttachmentChips
        attachments={props.attachments}
        items={items()}
        recalling={recalling()}
        lg={lg()}
      />
      <UploadNote failed={send.uploadFailed()} lg={lg()} />
      <StatusBar
        variant="recede"
        nowrap
        start={
          <>
            <AttachButton
              attachments={props.attachments}
              drop={drop}
              disabled={Boolean(props.disabled || recalling())}
            />
            {controls()}
          </>
        }
        end={
          <>
            {trailing()}
            <ComposerActions
              recalling={recalling()}
              recallSavable={Boolean(recalled()?.trim())}
              awaiting={send.awaiting()}
              canSend={send.canSend()}
              streaming={props.streaming}
              stoppable={props.stoppable}
              disabled={props.disabled}
              sendBlocked={props.sendBlocked}
              sendLabel={props.sendLabel}
              hasDraft={hasDraft()}
              onSave={saveRecall}
              onSubmit={send.submit}
              onCancelAwaiting={send.cancelAwaiting}
              onStop={send.stop}
            />
          </>
        }
      />
      <div class="sr-only" aria-live="polite" role="status">
        {announcement()}
      </div>
    </div>
  );
}
