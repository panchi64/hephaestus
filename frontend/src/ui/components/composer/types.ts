import type { Accessor, JSX } from "solid-js";
import type { ComposerAttachment } from "../AttachmentChip";
import type { ComposerMenuGroup, ComposerMenuItem } from "./ComposerMenu";
import type { ComposerTrigger } from "./composerToken";

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
   *  into the live run (mid-run steering); a disabled field shows STOP alone. */
  streaming?: boolean;
  /** Nothing is generating but something still runs that STOP ends (a thread's
   *  sub-agents): STOP joins SEND, which stays a plain send. */
  stoppable?: boolean;
  /** The run is waiting in line for capacity: the plate takes the queue tone, still. */
  queued?: boolean;
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
