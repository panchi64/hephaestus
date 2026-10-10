import {
  For,
  Show,
  children,
  createEffect,
  createSignal,
  untrack,
  type Accessor,
  type JSX,
} from "solid-js";
import { isMac } from "~/lib/keymap";
import { attachmentGate } from "~/lib/stores/sendGate";
import { cx } from "../cx";
import { Icon } from "../primitives/Icon";
import { Text } from "../primitives/Text";
import { useAutosize } from "../primitives/useAutosize";
import { HIDDEN_FILE_INPUT, useFileDrop } from "../primitives/useFileDrop";
import { AttachmentChip, type ComposerAttachment } from "./AttachmentChip";
import {
  ComposerMenu,
  type ComposerMenuGroup,
  type ComposerMenuItem,
} from "./ComposerMenu";
import { loadDraft, saveDraft } from "./composerDraft";
import { createComposerMenuController } from "./composerMenuController";
import { tokenSpans, type ComposerTrigger } from "./composerToken";
import { StatusBar, StatusCell } from "./StatusBar";
import { Tooltip } from "./Tooltip";

// The field rests at one line and grows a line at a time up to this many lines or
// this share of the window, whichever is smaller, then scrolls — a long prompt gets
// room to be written and read back without the unit swallowing the conversation.
const MAX_ROWS = 10;
const MAX_VIEWPORT_SHARE = 0.4;

// The send chord's glyph, for the button's hint. Read once: the platform does not
// change under a running page.
const IS_MAC = isMac();
const SEND_CHORD = IS_MAC ? "⌘↵" : "Ctrl↵";

/** A commit key's chord, after its label at reduced weight — on screen so the rule is
 *  read rather than remembered, and quieter than the word so the word is what's read
 *  first. Hidden from assistive tech: the button's `aria-keyshortcuts` says it. */
function KeyHint(props: { children: string }): JSX.Element {
  return <kbd aria-hidden="true">{props.children}</kbd>;
}

/** One token in the accent layer: the sigil cell welded to the name cell. A file's
 *  name splits at its last `/` so the directory can sit dim beside a bright basename. */
function Token(props: { trigger: ComposerTrigger; text: string }): JSX.Element {
  const name = () => props.text.slice(1);
  const cut = () => (props.trigger === "@" ? name().lastIndexOf("/") : -1);
  return (
    <span class="ody-tok" data-trigger={props.trigger}>
      <span class="ody-tok-sigil">{props.trigger}</span>
      <span class="ody-tok-name">
        <Show when={cut() >= 0} fallback={name()}>
          <span class="ody-tok-dir">{name().slice(0, cut() + 1)}</span>
          {name().slice(cut() + 1)}
        </Show>
      </span>
    </span>
  );
}

/** An IME is mid-composition: its Enter commits a candidate, and acting on it would
 *  send (or pick) half a word. `keyCode` 229 is the only signal some engines give. */
const composing = (e: KeyboardEvent): boolean =>
  e.isComposing || e.keyCode === 229;

/**
 * Attachment controller injected by the feature layer. The design system can't
 * reach the uploads data seam, so the Composer owns the *chips* but not the
 * upload/poll — the feature hands it this. The Composer reads `items` to render,
 * drives `attach`/`remove`/`toggleKbExcluded`/`retry` from the chip controls, reads
 * the ready ids on SEND, and calls `clear` after a send — having first taken a
 * `snapshot`, which it hands back to `restore` if the send is refused.
 */
export interface ComposerAttachmentsApi {
  items: Accessor<ComposerAttachment[]>;
  attach: (files: File[]) => void;
  remove: (id: string) => void;
  toggleKbExcluded: (id: string) => void;
  clear: () => void;
  /** Copies of the current chips, detached from the live list. */
  snapshot: () => ComposerAttachment[];
  /** Put a snapshot back, resuming progress tracking for anything still in flight. */
  restore: (items: ComposerAttachment[]) => void;
  /** Re-run a failed chip's upload or extraction. */
  retry: (id: string) => void;
}

/**
 * The `/` and `@` menu controller, injected by the feature layer — the same split as
 * `ComposerAttachmentsApi` above and for the same reason: the design system cannot reach
 * a data seam, so the Composer owns the *token* and the *keys* and the feature owns what
 * the rows are and what picking one means.
 *
 * The Composer calls `onQuery` whenever the token under the caret changes (and with
 * `null` when there is none), renders `groups`, and routes the navigation keys here
 * before applying its own rules for Enter and Tab.
 */
export interface ComposerMenuApi {
  /** Rows to show for the current query. Empty shows "No matches" while a token is
   *  active; the menu closes when the token goes. */
  groups: Accessor<ComposerMenuGroup[]>;
  /** The rows for the current query are still being fetched — the menu reads
   *  "Loading…" rather than "No matches" until they arrive. */
  loading?: Accessor<boolean>;
  /** The token under the caret changed. `null` means there is none — the menu closes. */
  onQuery: (token: { trigger: ComposerTrigger; query: string } | null) => void;
  /** What picking a row does, in three answers:
   *  - a **string** is inserted in place of the token, **without** its trigger
   *    character (`reviewer`, `src/app.tsx`);
   *  - **null** means the row acted instead of completing — an action command fired
   *    its relay — and the composer clears;
   *  - **undefined** means the row no longer exists (the list moved under the pick),
   *    so nothing happened: the menu goes and the draft stays exactly as it was. */
  onPick: (item: ComposerMenuItem) => string | null | undefined;
  /** Called after a pick that returned null, so the feature can clear the draft itself
   *  once its relay has fired. */
  onClear?: () => void;
}

/**
 * Recall controller, injected by the feature layer: ArrowUp in an empty field lends the
 * field to something already written, to be edited in place — the way a shell recalls
 * its history. The same split as the other two controllers: the Composer owns the keys,
 * the banner and the field, and the feature owns what is recalled and what saving it
 * means.
 *
 * While `draft()` is a string the field shows and edits it instead of the operator's own
 * draft, which is left exactly as it was — **not persisted over, not cleared** — and
 * comes back the moment the recall ends. The feature can end it underneath the Composer
 * (the recalled thing went away) simply by answering `null`.
 */
export interface ComposerRecallApi {
  /** The recalled text being edited, or `null` when nothing is recalled. */
  draft: Accessor<string | null>;
  /** What the banner says is being edited ("Editing queued message"). Also what is
   *  announced when a recall begins. */
  label: string;
  /** Walk the recall: `older` from ArrowUp (on an empty field, or with the caret at the
   *  very start of a recalled one), `newer` from ArrowDown at its very end. Returns
   *  whether anything moved — stepping out past the newest is a move. */
  step: (direction: "older" | "newer") => boolean;
  /** A keystroke in a recalled field. */
  write: (text: string) => void;
  /** Commit the edit and end the recall. */
  save: () => void;
  /** End the recall without committing. */
  cancel: () => void;
}

export interface ComposerProps {
  /** Receives the trimmed text and the ids of every ready attachment.
   *
   *  The field clears the moment this is called. A caller whose send can be refused
   *  returns a promise: resolving `false` says the message was not accepted, and the
   *  composer puts the text and the attachments back — the text only if the field is
   *  still empty, since the operator may already have started the next message. A
   *  message the product did not take must never be a message the operator has lost. */
  onSend: (text: string, attachmentIds: string[]) => void | Promise<boolean>;
  /** Shift+Tab in the field. When given, the key is taken (and backward focus
   *  traversal out of the field is given up for it — forward Tab still leaves);
   *  without it Shift+Tab is left to the browser. Never fires while the `/` or `@`
   *  menu is open. */
  onShiftTab?: () => void;
  /** The field's accessible name. Defaults to "Message". */
  label?: string;
  disabled?: boolean;
  /** Why this message cannot be sent, or null when it can. SEND is disabled and
   *  carries the reason on hover; the **field stays live**, so a draft already typed
   *  survives and can still be edited while the operator goes and fixes the cause.
   *
   *  Distinct from `disabled`, which means "the composer is not accepting input right
   *  now" (a run owns it). This means "what you have is fine, but it would be refused"
   *  — a blocker the operator can act on, which is why it carries an explanation and
   *  `disabled` doesn't. */
  sendBlocked?: string | null;
  /** A run is generating: a STOP button wired to `onStop` joins SEND beside the
   *  field, so the interrupt control sits where the user's focus already is, and
   *  Escape in the field is a plain stop. When the field itself stays enabled
   *  (`disabled` false), SEND remains beside it — the caller queues the message
   *  into the live run (mid-run steering); a disabled field shows STOP alone.
   *  Attaching, dropping and pasting files are unavailable while streaming. */
  streaming?: boolean;
  /**
   * Invoked when STOP is pressed mid-stream (see `streaming`), with whatever was in
   * the field at that moment — **stop and correct in one act**.
   *
   * Interrupting is almost never the whole intention. The operator stops a run because
   * it is doing the wrong thing, and what they want next is to say what the right thing
   * is; two controls made that two acts with a gap between them, and the gap is where
   * the correction gets retyped or lost. So the button carries the draft: type the
   * correction, press STOP, and the run ends and the correction goes.
   *
   * The composer decides nothing about it — it hands over the text and clears the
   * field. Whether that becomes the next turn, and what happens to the run's own
   * state, is the caller's, because it is a rule about what the product does.
   *
   * `undefined` when the field was empty, which is a plain interrupt; the field then
   * takes focus so the correction can simply be typed.
   */
  onStop?: (correction?: string) => void;
  /** What SEND is called. Defaults to "Send".
   *
   *  For the one case where sending this field does something other than message the
   *  agent: the approval dock borrows the composer to collect a request for changes, and
   *  a button reading "Send" beside a plan the operator has just rejected says the wrong
   *  thing about what is about to happen. A label, not a second component — everything
   *  else about the control, and about the field above it, is identical. */
  sendLabel?: string;
  /** Text to insert into the field programmatically (e.g. an undelivered queued
   *  message restored after a cancel). Applied whenever it becomes non-empty —
   *  appended below any current draft — then acknowledged via
   *  `onPrefillConsumed` so the caller can clear it. */
  prefill?: string | null;
  onPrefillConsumed?: () => void;
  placeholder?: string;
  /** `md` = docked input bar (default); `lg` = centered hero field. */
  size?: "md" | "lg";
  /** The plate's left label when `headerStart` gives none — the hero's
   *  "New thread · ready". Set in the plate's mono, like everything on it. */
  title?: string;
  /** The **left** of the unit's plate: what the thread is doing right now — its name
   *  and `ready` at rest, the live run's activity while one streams. The caller's,
   *  because what a run is doing is a fact the design system cannot reach.
   *
   *  One line of mono uppercase, dim at rest and info while `streaming`, unless a piece
   *  of it carries its own tone. While a message is recalled the Composer takes the
   *  whole line over for the edit's own label and keys — one plate, never a second line
   *  stacked on this one. */
  headerStart?: JSX.Element;
  /** The **right** of the plate — the run's clock while one streams, the branch at
   *  rest, queued messages waiting to be edited. When it renders nothing, a composer
   *  with a plate and a `storageKey` says `Draft saved` there while the field holds a
   *  persisted draft. */
  headerEnd?: JSX.Element;
  /** Something the thread is waiting on the operator for, said where they are about to
   *  type: the plate is **replaced** by the action tier's amber strip (`label`, e.g.
   *  "1 approval waiting"), and `Jump ↑` takes them to the ask. Null when nothing is. */
  waiting?: { label: string; onJump?: () => void } | null;
  autofocus?: boolean;
  /** Persists the unsent draft to localStorage under this key, reactively —
   *  switching keys (e.g. between conversations) loads that key's draft. */
  storageKey?: string;
  /** Cells at the **start** of the status bar under the card, after `+ file` — the
   *  settings that describe what this message *is* (its level, its model). Each direct
   *  child is one `StatusBar` cell: a `StatusCell`, or a picker with `cell` set. */
  controls?: JSX.Element;
  /** Cells at the **end** of the status bar — the state of the thread the message is
   *  going into (its tasks, its stats, how full its context window is).
   *
   *  Two slots rather than one because the bar reads left to right as a sentence:
   *  what this is, then where it's going. Right-aligned, so on a narrow screen where
   *  the bar wraps the trailing group keeps its edge rather than trailing the leading
   *  one. */
  trailing?: JSX.Element;
  /** Drop the unit's fill — **the frame is kept**. For a composer nested inside another
   *  surface (the approval dock): a fill on a fill is the box-in-a-box §7 exists to
   *  stop. */
  bare?: boolean;
  /** File-attachment controller. When supplied, the Composer shows an attach
   *  button + drag-drop and renders the attachment chips; omit to hide them. */
  attachments?: ComposerAttachmentsApi;
  /** `/` and `@` menu controller. Omit and neither character does anything special,
   *  which is what the compare bench and the approval dock want. */
  menu?: ComposerMenuApi;
  /** ArrowUp-to-edit controller. Omit and the arrows are the field's own. */
  recall?: ComposerRecallApi;
  class?: string;
}

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
  // The accent layer behind the field. Held so its scroll can be kept in step with the
  // field's — past `MAX_ROWS` the textarea scrolls, and a layer that stayed put would
  // paint the colours of the first lines over whatever had scrolled into view.
  let highlightRef: HTMLDivElement | undefined;

  // ── The `/` and `@` menu ────────────────────────────────────────────────────────
  // Its state and keys are `composerMenuController`'s; what the rows *are*, and what
  // picking one means, is the feature's, through `props.menu`. A recalled message is
  // edited as the words it was sent with — a `/` in it is text, not a command, since
  // the edit goes back to the queue rather than through a send.
  const menuCtl = createComposerMenuController({
    menu: () => props.menu,
    field: () => field,
    text,
    setText,
    suspended: recalling,
  });
  const menuOpen = menuCtl.open;
  const syncToken = menuCtl.sync;
  const dismissMenu = menuCtl.dismiss;

  const items = () => props.attachments?.items() ?? [];
  const readyIds = () =>
    items()
      .filter((a) => a.status === "ready")
      .map((a) => a.id);

  // Files arrive by drop and paste only while the attach button would take them. A
  // file dropped mid-run used to become a chip that STOP & correct then threw away.
  const drop = useFileDrop(
    (files) => props.attachments?.attach(files),
    () => !props.disabled && !props.streaming && !recalling(),
  );

  // ── Screen-reader announcements ────────────────────────────────────────────────
  // One polite live region for the state changes that otherwise only show up as a
  // label swap somewhere the operator isn't looking. Cleared and re-set on the next
  // tick so the same sentence twice is announced twice.
  const [announcement, setAnnouncement] = createSignal("");
  const announce = (message: string): void => {
    setAnnouncement("");
    queueMicrotask(() => setAnnouncement(message));
  };
  let wasStreaming = untrack(() => props.streaming) ?? false;
  createEffect(() => {
    const streaming = props.streaming ?? false;
    if (streaming && !wasStreaming)
      announce(
        untrack(() => props.disabled)
          ? "Agent running"
          : `Agent running — ${SEND_CHORD} queues, Esc stops`,
      );
    wasStreaming = streaming;
  });
  createEffect(() => {
    const reason = props.sendBlocked;
    if (reason) announce(reason);
  });
  // The banner says it to a sighted operator; this says it to everyone else. On the
  // entering edge only — walking from one recalled message to the next is the same mode.
  let wasRecalling = false;
  createEffect(() => {
    const now = recalling();
    if (now && !wasRecalling)
      announce(untrack(() => props.recall?.label) ?? "Editing");
    wasRecalling = now;
  });

  // Load the draft for the active key — runs on mount and whenever the key
  // changes (e.g. switching conversations). This effect is the sole owner of
  // key transitions: it swaps `text` to the incoming key's draft.
  createEffect(() => {
    const key = props.storageKey;
    setText(key ? loadDraft(key) : "");
  });

  // Persist edits back to the active key. Tracks only `text` — the key is read
  // untracked so a key change never writes the outgoing draft under the incoming
  // key (the load effect above already swapped `text`). Tracking the key here
  // would race that load and leak a stale draft across conversations.
  createEffect(() => {
    const value = text();
    const key = untrack(() => props.storageKey);
    if (key) saveDraft(key, value);
  });

  // Focus the field on mount and again whenever the active conversation changes
  // (the key transition), so a freshly opened or newly started chat is ready to
  // type into without a click. Reading `storageKey` tracked makes the refocus
  // fire on switch; `autofocus` is read untracked so only opted-in callers grab
  // focus and a key change alone never enables it.
  createEffect(() => {
    // Returned (rather than discarded) so reading the key counts as a tracked
    // dependency — the refocus then fires on every conversation switch.
    const key = props.storageKey;
    if (untrack(() => props.autofocus)) field?.focus();
    return key;
  });

  // Re-focus when the field re-enables after a run finishes (disabled true →
  // false), so the conversation continues without a click. Gated on `autofocus`
  // — same opt-in as the mount/switch focus above — and only on the enabling
  // edge, so an idle field toggling for any other reason doesn't grab focus.
  let wasDisabled = untrack(() => props.disabled) ?? false;
  createEffect(() => {
    const disabled = props.disabled ?? false;
    if (wasDisabled && !disabled && untrack(() => props.autofocus))
      field?.focus();
    wasDisabled = disabled;
  });

  // Apply a caller-provided prefill (restored undelivered text): append below
  // any current draft, hand focus back, and acknowledge so the caller clears it.
  createEffect(() => {
    const incoming = props.prefill;
    if (!incoming) return;
    setText((current) => (current ? `${current}\n${incoming}` : incoming));
    props.onPrefillConsumed?.();
    field?.focus();
  });

  // A chip that is uploading or extracting counts toward "something to send": the
  // send is then held (below) rather than refused. A failed one doesn't — it can't go.
  const hasAttachment = () => items().some((a) => a.status !== "error");
  const canSend = () =>
    !props.disabled &&
    !props.sendBlocked &&
    (Boolean(text().trim()) || hasAttachment());

  // ── Hold-then-send ─────────────────────────────────────────────────────────────
  // SEND with files still in flight holds the message and fires it the moment they
  // land. Sending without them silently dropped the file; refusing would make the
  // operator watch a chip and press SEND a second time. The field stays editable
  // while it waits, and a second press (or Escape) calls the wait off.
  const [awaiting, setAwaiting] = createSignal(false);
  // The inline note when a held file fails — the chip says which, this says why the
  // message didn't go.
  const [uploadFailed, setUploadFailed] = createSignal(false);
  const gate = () => attachmentGate(items());

  createEffect(() => {
    // A failure that has been retried or removed takes its note with it.
    if (gate() !== "failed") setUploadFailed(false);
  });

  createEffect(() => {
    if (!awaiting()) return;
    const state = gate();
    if (state === "pending") return;
    if (state === "failed") {
      setAwaiting(false);
      setUploadFailed(true);
      return;
    }
    // Everything landed. If the message can no longer go — a blocker appeared, the
    // field was emptied — the wait simply ends; the draft is still in the field.
    untrack(() => (canSend() ? submit() : setAwaiting(false)));
  });

  // A held send belongs to the conversation it was pressed in. Switching away calls
  // it off, or the uploads landing later would send the *new* conversation's draft.
  createEffect(() => {
    void props.storageKey;
    setAwaiting(false);
  });

  const cancelAwaiting = (): void => {
    setAwaiting(false);
    field?.focus();
  };

  /** Restore a refused send under the key it was sent from — a refusal that lands
   *  after a conversation switch goes back into *that* conversation's draft rather
   *  than into whichever one is open now. Put **above** anything written since, the
   *  way a prefill joins a draft: the operator may already be on the next message,
   *  and neither text is theirs to lose. */
  const restoreRefused = (
    draft: string,
    key: string | undefined,
    saved: ComposerAttachment[],
  ): void => {
    const join = (current: string) =>
      current ? `${draft}\n${current}` : draft;
    if (key === untrack(() => props.storageKey)) {
      setText(join);
      if (saved.length && !untrack(items).length)
        props.attachments?.restore(saved);
      field?.focus();
    } else if (key) saveDraft(key, join(loadDraft(key)));
    announce("Message not sent — restored");
  };

  const submit = () => {
    if (!canSend()) return;
    const state = gate();
    if (state === "failed") {
      setUploadFailed(true);
      return;
    }
    if (state === "pending") {
      setAwaiting(true);
      announce("Waiting for uploads");
      return;
    }
    setAwaiting(false);
    const draft = text();
    const key = props.storageKey;
    const saved = props.attachments?.snapshot() ?? [];
    const result = props.onSend(draft.trim(), readyIds());
    setText(""); // clears the persisted draft via the effect above
    props.attachments?.clear();
    field?.focus();
    if (result instanceof Promise)
      void result.then(
        (accepted) => {
          if (accepted === false) restoreRefused(draft, key, saved);
        },
        // A send that threw was not accepted either.
        () => restoreRefused(draft, key, saved),
      );
  };

  /** Commit a recalled edit. An emptied one is not a save — SAVE is disabled over it —
   *  since taking a message back is a withdraw, and that is not what this key says. */
  const saveRecall = (): void => {
    if (!recalled()?.trim()) return;
    props.recall?.save();
    announce("Saved");
    field?.focus();
  };

  /** Walk the recall and land the caret at the end of whatever the field now shows —
   *  after the DOM has the new value, or it lands at the old length. */
  const stepRecall = (direction: "older" | "newer"): void => {
    if (!props.recall?.step(direction)) return;
    queueMicrotask(() => {
      const end = field?.value.length ?? 0;
      field?.setSelectionRange(end, end);
    });
  };

  /** The recall's keys, taken after the menu's and before the Composer's own rules — so
   *  Escape here cancels the edit rather than stopping the run, and ⌘/Ctrl+Enter saves
   *  it rather than sending. The arrows only fire at the field's edges: ArrowUp on an
   *  empty field or with the caret at the very start, ArrowDown at the very end. Anywhere
   *  else they move the caret through a multi-line message as they always have. */
  const recallKey = (e: KeyboardEvent): boolean => {
    const recall = props.recall;
    if (!recall || !field || composing(e)) return false;
    const plain = !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey;
    const { selectionStart: start, selectionEnd: end } = field;
    if (e.key === "ArrowUp" && plain) {
      const atEdge = recalling()
        ? start === 0 && end === 0
        : text() === "" && !awaiting() && !props.disabled;
      if (!atEdge) return false;
      e.preventDefault();
      stepRecall("older");
      return true;
    }
    if (!recalling()) return false;
    if (e.key === "ArrowDown" && plain) {
      const length = field.value.length;
      if (start !== length || end !== length) return false;
      e.preventDefault();
      stepRecall("newer");
      return true;
    }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      saveRecall();
      return true;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      recall.cancel();
      field.focus();
      return true;
    }
    return false;
  };

  const onKeyDown = (e: KeyboardEvent) => {
    // The menu gets first refusal. Enter picks a row rather than adding a line —
    // without this the operator's `/rev` would never complete.
    if (menuCtl.onKey(e, composing(e))) return;
    if (recallKey(e)) return;
    if (e.key === "Enter") {
      // ⌘/Ctrl+Enter sends; a bare Enter or Shift+Enter is left to the field and
      // inserts a newline, so a stray Enter can't split one message into two sends.
      if (!(e.metaKey || e.ctrlKey) || composing(e)) return;
      e.preventDefault();
      // Pressing it again while the send is held is the same intent, not a cancel.
      if (!awaiting()) submit();
      return;
    }
    if (e.key === "Escape") {
      // Innermost first: a held send, then the run. Stopped from propagating either
      // way, because Escape here meant this and not the panel behind the composer.
      if (awaiting()) {
        e.preventDefault();
        e.stopPropagation();
        cancelAwaiting();
      } else if (props.streaming && props.onStop) {
        e.preventDefault();
        e.stopPropagation();
        // A plain stop: whatever is in the field stays there. STOP & correct is the
        // button, where the label says the draft is about to go.
        props.onStop(undefined);
      }
      return;
    }
    if (e.key === "Tab" && e.shiftKey && props.onShiftTab) {
      e.preventDefault();
      props.onShiftTab();
    }
  };

  const lg = () => props.size === "lg";

  // Grow the field to fit what it shows — a recall swapping a message in included.
  // Both sizes grow: the hero is told apart by its width and padding, not by empty
  // rows.
  useAutosize(() => field, value, {
    maxRows: MAX_ROWS,
    maxViewportShare: MAX_VIEWPORT_SHARE,
  });

  /* The field carries no chrome of its own — no border, no fill, no focus ring.
     The card around it is the control, and it is what lights up on focus, so a
     second bordered box inside it would only add a line (§7). */
  const fieldClass = () =>
    cx(
      /* `text-prose`, the reading scale — the field produces the operator's turn,
         which renders at reading size a few pixels above it. Typing at 13px and
         watching it come back at 16px is the same mismatch, one step earlier. */
      /* `block`, because a textarea is inline-block and carries a baseline descender —
         ~5px of dead space under it inside any block container. It cost nothing in the
         flex column this used to sit in directly, and it is load-bearing now that the
         field shares a wrapper with the accent layer measured against it. */
      /* No `min-h-*`: the field rests at one row (`rows={1}`) at both sizes and
         grows from there, so an empty composer is one line of text. */
      "block w-full resize-none border-0 bg-transparent text-prose font-sans text-bright placeholder:text-dim outline-none disabled:opacity-40",
      lg() ? "px-4 py-3" : "px-3 py-2.5",
    );

  /** The field's text split into plain runs and tokens (`Token`, `.ody-tok`).
   *
   *  A textarea cannot colour part of its own value, so the standard arrangement is used:
   *  this layer sits directly behind a text-transparent field, painting the same string
   *  with the same metrics, and the operator sees the caret and selection of the real
   *  control over the colours of this one.
   *
   *  Everything about it is therefore metric-bound to the field, which is why it shares
   *  `fieldClass()` rather than restating the typography: any difference in font, size,
   *  padding or wrapping shows up immediately as the colour drifting off the words. It
   *  also has to render a trailing newline as a space, since a `<div>` collapses one that
   *  a textarea shows — without it the last line scrolls out of step.
   */
  const highlighted = () => {
    const shown = value();
    // No accents on a recalled message: its tokens are not being resolved (see
    // `syncToken`), and colouring them would say they were.
    const spans = props.menu && !recalling() ? tokenSpans(shown) : [];
    const runs: { text: string; trigger: ComposerTrigger | null }[] = [];
    let at = 0;
    for (const span of spans) {
      if (span.start > at)
        runs.push({ text: shown.slice(at, span.start), trigger: null });
      runs.push({
        text: shown.slice(span.start, span.end),
        trigger: span.trigger,
      });
      at = span.end;
    }
    if (at < shown.length) runs.push({ text: shown.slice(at), trigger: null });
    return runs;
  };

  const highlightLayer = (
    <Show when={props.menu}>
      <div
        aria-hidden="true"
        ref={(el) => {
          highlightRef = el;
        }}
        class={cx(
          fieldClass(),
          // Behind the field, exactly on top of it, and inert: the real control takes
          // every click, caret move and selection. `isolate` keeps each token's box
          // (painted at `z-index: -1`) behind its text but above the card's fill.
          "pointer-events-none absolute inset-0 isolate overflow-hidden whitespace-pre-wrap break-words text-bright",
        )}
      >
        <For each={highlighted()}>
          {(run) => (
            <Show when={run.trigger} fallback={run.text}>
              {(trigger) => <Token trigger={trigger()} text={run.text} />}
            </Show>
          )}
        </For>
        {/* A textarea shows a trailing newline; a div collapses it. */}
        {value().endsWith("\n") ? " " : ""}
      </div>
    </Show>
  );

  const textarea = (
    <textarea
      ref={field}
      value={value()}
      onInput={(e) => {
        const next = e.currentTarget.value;
        if (recalling()) return props.recall?.write(next);
        setText(next);
        syncToken();
      }}
      onKeyDown={onKeyDown}
      // Arrow keys and clicks move the caret without changing the text, and moving back
      // into a half-typed `/rev` is the same situation as having just typed it.
      onKeyUp={syncToken}
      onClick={syncToken}
      // The menu hangs off the field's content, so it goes when the field does. This is
      // also what replaces the backdrop `FloatingPanel` would otherwise draw over the
      // whole screen — see its `passive` prop.
      onBlur={dismissMenu}
      onScroll={(e) => {
        if (highlightRef) highlightRef.scrollTop = e.currentTarget.scrollTop;
      }}
      onPaste={props.attachments ? drop.pasteHandlers.onPaste : undefined}
      rows={1}
      // No chord here: SEND already carries it, and at a docked width the longer
      // placeholder wrapped the resting field onto a second row.
      placeholder={props.placeholder ?? "Message the agent…"}
      aria-label={
        recalling() ? props.recall?.label : (props.label ?? "Message")
      }
      disabled={props.disabled}
      role={props.menu ? "combobox" : undefined}
      aria-expanded={props.menu ? menuOpen() : undefined}
      aria-controls={props.menu ? "composer-menu" : undefined}
      // Focus stays here the whole time, so this is the only thing that tells a screen
      // reader which row the arrows are on.
      aria-activedescendant={
        menuOpen() ? (menuCtl.activeId() ?? undefined) : undefined
      }
      class={cx(
        fieldClass(),
        // The field's own glyphs go transparent so the coloured layer behind shows
        // through; the caret keeps a colour of its own, or typing would be invisible.
        // `relative` so it stacks above that layer rather than under it.
        props.menu && "relative bg-transparent text-transparent caret-bright",
      )}
    />
  );

  // The attach affordance: a button that opens the picker plus the hidden input
  // the file-drop hook clicks. Only mounted when an attachments controller is
  // wired, so non-attachment surfaces are unchanged.
  //
  // A plus, not a paperclip: what it means at the head of the strip is "add something
  // to this message", and a `+` says that at a glance. Disabled rather than hidden while
  // a run streams or a message is recalled, so the strip never reflows under the
  // pointer.
  const attachBtn = (
    <Show when={props.attachments}>
      <Tooltip label="Attach files" side="top" delay={600}>
        <StatusCell
          aria-label="Attach files"
          disabled={props.disabled || props.streaming || recalling()}
          onClick={drop.openPicker}
          class="px-3"
        >
          <Icon name="plus" size={14} />
        </StatusCell>
      </Tooltip>
    </Show>
  );
  // The picker's hidden input lives in the card, not beside the cell: the bar rules
  // its groups' direct children, so a sibling input there would draw a hairline of its
  // own, and inside the button its programmatic click would bubble back into it.
  const fileInput = (
    <Show when={props.attachments}>
      <input
        ref={drop.bindInput}
        {...HIDDEN_FILE_INPUT}
        {...drop.inputHandlers}
      />
    </Show>
  );

  // Attachment chips, under the field: square mono chips, each with its type, its
  // status, a KB-membership toggle, a remove control, and — once failed — its reason
  // and a retry. Not over a recalled message: the chips belong to the operator's own
  // draft, which is off screen until the recall ends, and shown here they would read
  // as riding the edit.
  const chips = (
    <Show when={props.attachments && !recalling() && items().length > 0}>
      <div class={cx("flex flex-wrap gap-1.5 pb-2.5", lg() ? "px-4" : "px-3")}>
        <For each={items()}>
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

  // Why a held send stopped holding. Inline in the card, beside the chips it is
  // about, rather than a toast: the fix — retry or remove — is right there.
  const uploadNote = (
    <Show when={uploadFailed()}>
      <div role="alert" class={cx("pb-2", lg() ? "px-4" : "px-3")}>
        <Text variant="micro" tone="alert">
          An upload failed — retry or remove it
        </Text>
      </div>
    </Show>
  );

  // While a run streams, STOP joins SEND so the interrupt sits where the user's
  // focus already is. A caller that keeps the field enabled mid-stream keeps SEND
  // beside it (⌘↵/SEND then queues into the live run — steering); one that disables
  // the field shows STOP alone, as before.
  // A component, not a JSX value held in a variable: each `<SendButton />` below builds
  // its own element. Sharing one across both arms of the `Show` shares the same DOM
  // node, and Solid tears the tree apart trying to move it between them ("the new child
  // element contains the parent") the first time the block state flips.
  //
  // While a send is held the button reads what it is doing and a press calls it off —
  // enabled even though nothing new can be sent, because cancelling is the one thing
  // left to do with it. The chord hint is dropped then: it would say "press this to
  // send" over a button that no longer sends.
  //
  // While a message is recalled the same button reads SAVE and commits the edit: it is
  // the same chord, and the same "this is done, let it go" — only what it does differs.
  // A send gate says nothing about an edit to a message already sent, so it is neither
  // consulted nor shown then.
  //
  // Mid-run the same key reads QUEUE, because that is what it does: the message waits
  // for the run's next step rather than starting a turn. A label, not a second button.
  const sendLabel = () =>
    recalling()
      ? "Save"
      : props.streaming
        ? "Queue"
        : (props.sendLabel ?? "Send");
  // Outlined, never a white slab under the words (`.ody-send`): dim while there is
  // nothing to send — which is what `disabled` already says — bright once there is,
  // filled only under the pointer.
  const SendButton = () => (
    <button
      type="button"
      class="ody-send"
      disabled={recalling() ? !recalled()?.trim() : !awaiting() && !canSend()}
      onClick={() =>
        recalling() ? saveRecall() : awaiting() ? cancelAwaiting() : submit()
      }
      aria-keyshortcuts={IS_MAC ? "Meta+Enter" : "Control+Enter"}
    >
      <Show when={recalling() || !awaiting()} fallback="Waiting for upload…">
        {sendLabel()}
        <KeyHint>{SEND_CHORD}</KeyHint>
      </Show>
    </button>
  );
  // Wrapped only when there is something to say. A tooltip on every SEND would fire on
  // the one control the operator uses most, to tell them nothing.
  const sendBtn = () => (
    <Show when={!recalling() && props.sendBlocked} fallback={<SendButton />}>
      {(reason) => (
        <Tooltip label={reason()} side="top">
          <SendButton />
        </Tooltip>
      )}
    </Show>
  );
  // Stop, carrying the draft when there is one. The label says which of the two it is
  // about to be, because "Stop" over a field with a correction in it would give no
  // warning that pressing it also sends the correction — and the operator who typed one
  // and then wanted a plain interrupt would have no way to tell.
  const stop = () => {
    setAwaiting(false);
    const correction = text().trim();
    props.onStop?.(correction || undefined);
    if (correction) {
      setText(""); // clears the persisted draft via the effect above
      props.attachments?.clear();
    } else field?.focus(); // nothing to say yet — put the caret where saying it happens
  };
  // The tip names Escape, the stop that never carries the draft — which is also how
  // an operator with a correction typed can still interrupt without sending it.
  const stopTip = () =>
    text().trim()
      ? "Stop the run and send this correction (Esc stops without sending)"
      : "Stop the run (Esc)";
  // The commit keys, at the strip's end. `flex` so the cluster is one cell of the
  // strip, its keys standing inside it with their own outline.
  const actionBtn = (
    <span class="flex shrink-0 items-stretch">
      <Show when={props.streaming} fallback={sendBtn()}>
        <Show when={!props.disabled}>{sendBtn()}</Show>
        {/* Alert: it ends work in flight, and with a correction typed it also sends
            it — the one key on the unit with a consequence the operator cannot take
            back. */}
        <Tooltip label={stopTip()} side="top">
          <button
            type="button"
            class="ody-send"
            data-tone="stop"
            onClick={stop}
          >
            <span class="ody-send-sq" aria-hidden="true" />
            {text().trim() ? "Stop & correct" : "Stop"}
            <KeyHint>esc</KeyHint>
          </button>
        </Tooltip>
      </Show>
    </span>
  );

  // The key hint, in the strip rather than on the button alone: Enter is a newline
  // here, and that is the rule a newcomer would otherwise learn by sending half a
  // message. While a run streams the same chord queues; a field that is off says
  // nothing about keys it does not take.
  const keyHint = () =>
    recalling() || props.disabled
      ? undefined
      : `↵ new line · ${SEND_CHORD} ${props.streaming ? "queue" : "send"}`;

  // While files hover the unit: the console's own vocabulary — a bright ruled frame
  // and a plate — rather than a dashed tinted zone. Square, like the unit it covers.
  const dropOverlay = (
    <Show when={props.attachments && drop.isDragging()}>
      <div class="ody-composer-drop">
        <Text variant="plate" tone="bright">
          Drop to attach
        </Text>
      </div>
    </Show>
  );

  /* ONE layout for both sizes, and ONE ruled unit (§10.12): a square `line-strong`
     frame holding, top to bottom, the plate (what the thread is doing), the field
     (the full width — nothing sits beside it, so it never wraps around a button),
     the attachment chips, and the receded strip: `+`, the caller's pickers, then
     the key hint, the caller's thread state and the commit keys. Only padding
     differs between the sizes.

     The unit is marked as the point of action AT REST, not on hover or focus (§6):
     its opaque fill and its `line-strong` frame. No corner ticks, no shadow and no
     light — its separation from the transcript is the fill and the ruled edge (§13,
     shadows are for things that float). A live run is said in the plate, which
     turns info with a blinking square. */
  // Portalled to the body by `FloatingPanel`, so it sits outside the composer's own
  // stacking context and outside the transcript's `overflow-auto` — the two things that
  // would otherwise clip it. Mounted inside the card only so it shares the card's
  // lifetime.
  const menu = (
    <Show when={props.menu}>
      <ComposerMenu
        open={menuOpen()}
        groups={menuCtl.groups()}
        anchor={menuCtl.anchor}
        activeId={menuCtl.activeId()}
        loading={menuCtl.loading()}
        onPick={menuCtl.pick}
        onActivate={(item) => menuCtl.setActiveId(item.id)}
      />
    </Show>
  );

  // ── The header line ─────────────────────────────────────────────────────────────
  // One line of engraved mono across the top of the card: what the input is doing on
  // the left, what is waiting on the operator on the right. The caller fills both —
  // except while a message is recalled, when the Composer takes the whole line over.
  //
  // That takeover is the one line that says the field is not the operator's draft right
  // now, and it replaces the caller's line rather than stacking under it: the tell has
  // to be where the eye already is, and two header lines would make the unit taller at
  // exactly the moment its content is a single message being touched up. Dim, not an
  // accent: a mode is a state of the input, not a signal (§5), and the pen glyph plus
  // the SAVE key already carry it.
  //
  // Each slot resolved once — a JSX prop is a getter that rebuilds its elements on
  // every read, and a slot tested for emptiness and then rendered would mount twice.
  const headerStart = children(() => props.headerStart);
  const headerEnd = children(() => props.headerEnd);
  const headerLeft = () => headerStart() ?? props.title;
  // With nothing of the caller's to say on the right, the line says the draft is kept:
  // the words in the field are persisted under `storageKey` as they are typed, and an
  // operator about to navigate away is owed knowing that. Only on a composer that
  // already has a header — one that grew a line on the first keystroke would jump.
  const headerRight = () =>
    headerEnd() ??
    (headerLeft() && props.storageKey && text().trim()
      ? "Draft saved"
      : undefined);
  // The plate: the 7px square, what the thread is doing, and its aside. Its tone is
  // the unit's (`.ody-composer[data-live]` turns it info while a run streams); a piece
  // the caller hands in may carry its own.
  const plate = (
    <Show when={recalling() || headerLeft() || headerRight()}>
      <div class="ody-composer-plate">
        <span class="ody-composer-plate-sq" aria-hidden="true" />
        <Show
          when={recalling()}
          fallback={
            <>
              <span class="min-w-0 truncate">{headerLeft()}</span>
              <span class="ody-composer-plate-end">{headerRight()}</span>
            </>
          }
        >
          <span class="inline-flex min-w-0 items-center gap-1.5 truncate">
            <Icon name="pen" size={10} />
            {props.recall?.label}
          </span>
          <span class="ody-composer-plate-end">
            {SEND_CHORD} save · Esc cancel
          </span>
        </Show>
      </div>
    </Show>
  );

  // The action tier's echo: while the thread waits on the operator, the plate is the
  // amber strip — never under it, one line either way. A recalled message keeps its
  // own plate: the edit is what the operator is doing right now.
  const header = (
    <Show when={!recalling() && props.waiting} fallback={plate}>
      {(waiting) => (
        <div class="ody-composer-wait" role="status">
          <span class="ody-action-pulse" aria-hidden="true" />
          <span class="min-w-0 truncate">{waiting().label}</span>
          <Show when={waiting().onJump}>
            {(jump) => (
              <button type="button" onClick={() => jump()()}>
                Jump ↑
              </button>
            )}
          </Show>
        </div>
      )}
    </Show>
  );

  const body = (
    <>
      {dropOverlay}
      {menu}
      {fileInput}
      {header}
      {/* The two are one control: the layer paints the words, the field owns the
          caret, and they must occupy the same box for the colours to land on the
          right glyphs. The gap this wrapper would otherwise add is closed on the field
          itself (`block` in `fieldClass`), not here: a textarea is inline-block, so a
          block wrapper puts ~5px of baseline descender under it. The layer is
          `inset-0` of this wrapper, so those 5px made it taller than the field, and
          once it scrolled the two moved by different amounts and the colours slid off
          the words. */}
      <div class="relative min-w-0">
        {highlightLayer}
        {textarea}
      </div>
      {chips}
      {uploadNote}
    </>
  );

  // `+`, then the caller's two groups, as the unit's receded strip — with the key hint
  // and the commit keys at its end, so the strip is always drawn: Send lives there.
  // Each slot resolved once, for the same reason as the header's.
  const controls = children(() => props.controls);
  const trailing = children(() => props.trailing);
  const strip = (
    <StatusBar
      variant="recede"
      start={
        <>
          {attachBtn}
          {controls()}
        </>
      }
      end={
        <>
          <Show when={keyHint()}>
            {(hint) => (
              <StatusCell class="max-sm:hidden" aria-hidden="true">
                {hint()}
              </StatusCell>
            )}
          </Show>
          {trailing()}
          {actionBtn}
        </>
      }
    />
  );

  const dropAttrs = () => (props.attachments ? drop.dropHandlers : {});

  // `class` lands on the unit — it is layout glue, and the thing being placed is the
  // whole unit. `ody-framed`: on a page carrying the deep field the composer's fill
  // goes glass, so the field reads *through* the screen's focal object instead of
  // stopping dead at its edge (§11.1). A bare composer has no fill to trade. The whole
  // unit is the drop target: it is where the operator is already looking.
  return (
    <div
      class={cx(
        "ody-composer",
        !props.bare && "ody-framed bg-surface",
        props.class,
      )}
      data-live={
        props.streaming && !props.waiting && !recalling() ? "" : undefined
      }
      data-bare={props.bare ? "" : undefined}
      {...dropAttrs()}
    >
      {body}
      {strip}
      <div class="sr-only" aria-live="polite" role="status">
        {announcement()}
      </div>
    </div>
  );
}
