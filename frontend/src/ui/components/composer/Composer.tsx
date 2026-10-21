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
 * localStorage (per `storageKey`) and restore on return, so an interrupted or
 * resumed message is never lost. Cosmetic difference between the docked bar and
 * the hero field is the `size` prop — never a forked component. When an
 * `attachments` controller is supplied, files can be attached (drop, paste or
 * pick) and ride along with the message; a send with files still uploading is
 * held until they land rather than sent without them.
 */
export function Composer(props: ComposerProps): JSX.Element {
  const [text, setText] = createSignal("");
  // What the field shows: a recalled message while one is open, else the operator's own
  // draft. Two values rather than one swapped in and out, so the draft never has to be
  // stashed, restored or kept from being persisted over — it is simply not on screen.
  const recalled = () => props.recall?.draft() ?? null;
  const recalling = () => recalled() !== null;
  const value = () => recalled() ?? text();
  let field: HTMLTextAreaElement | undefined;
  const getField = () => field;
  const lg = () => props.size === "lg";

  // ── The `/` and `@` menu ────────────────────────────────────────────────────────
  // Its state and keys are `composerMenuController`'s; what the rows *are*, and what
  // picking one means, is the feature's, through `props.menu`. A recalled message is
  // edited as the words it was sent with — a `/` in it is text, not a command, since
  // the edit goes back to the queue rather than through a send.
  const menuCtl = createComposerMenuController({
    menu: () => props.menu,
    field: getField,
    text,
    setText,
    suspended: recalling,
  });

  const items = () => props.attachments?.items() ?? [];

  // Files arrive by drop and paste only while the attach button would take them. Mid-run
  // they ride a queued send, which becomes a turn of its own after the live one.
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

  // `+`, then the caller's two groups, as the unit's receded strip — with the commit
  // keys at its end, so the strip is always drawn: Send lives there.
  // Each slot resolved once, for the same reason as the header's.
  const controls = children(() => props.controls);
  const trailing = children(() => props.trailing);

  const dropAttrs = () => (props.attachments ? drop.dropHandlers : {});

  /* ONE layout for both sizes, and ONE ruled unit (§10.12): a square `line-strong`
     frame holding, top to bottom, the plate (what the thread is doing), the field
     (the full width — nothing sits beside it, so it never wraps around a button),
     the attachment chips, and the receded strip: `+`, the caller's pickers, then
     the caller's thread state and the commit keys. Only padding
     differs between the sizes.

     The unit is marked as the point of action AT REST, not on hover or focus (§6):
     its opaque fill and its `line-strong` frame. No corner ticks, no shadow and no
     light — its separation from the transcript is the fill and the ruled edge (§13,
     shadows are for things that float). A live run is said in the plate, which
     turns info with a blinking square. */
  // `class` lands on the unit — it is layout glue, and the thing being placed is the
  // whole unit. `heph-framed`: on a page carrying the dot field the composer's fill
  // goes glass, so the field reads *through* the screen's focal object instead of
  // stopping dead at its edge (§11.1). A bare composer has no fill to trade. The whole
  // unit is the drop target: it is where the operator is already looking.
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
      {/* Portalled to the body by `FloatingPanel`, so it sits outside the composer's
          own stacking context and outside the transcript's `overflow-auto` — the two
          things that would otherwise clip it. Mounted inside the card only so it
          shares the card's lifetime. */}
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
