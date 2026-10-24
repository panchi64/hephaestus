import type { Accessor, JSX } from "solid-js";
import type { ComposerAttachment } from "../AttachmentChip";
import type { ComposerMenuGroup, ComposerMenuItem } from "./ComposerMenu";
import type { ComposerTrigger } from "./composerToken";

/**
 * Attachment controller injected by the feature layer: the design system can't reach the
 * uploads seam, so the Composer owns the chips but not the upload/poll. It reads `items`,
 * drives `attach`/`remove`/`toggleKbExcluded`/`retry` from the chip controls, reads the
 * ready ids on SEND, and calls `clear` after a send, having taken a `snapshot` it hands
 * back to `restore` if the send is refused.
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
 * The `/` and `@` menu controller, injected by the feature layer for the same reason as
 * `ComposerAttachmentsApi`: the Composer owns the token and the keys, the feature owns
 * what the rows are and what picking one means.
 *
 * The Composer calls `onQuery` whenever the token under the caret changes (`null` when
 * none), renders `groups`, and routes navigation keys here before its own Enter and Tab rules.
 */
export interface ComposerMenuApi {
  /** Rows for the current query. Empty shows "No matches" while a token is active; the
   *  menu closes when the token goes. */
  groups: Accessor<ComposerMenuGroup[]>;
  /** Rows are still being fetched; the menu reads "Loading…" instead of "No matches". */
  loading?: Accessor<boolean>;
  /** The token under the caret changed; `null` means none and the menu closes. */
  onQuery: (token: { trigger: ComposerTrigger; query: string } | null) => void;
  /** What picking a row does:
   *  - a string replaces the token, without its trigger character (`reviewer`, `src/app.tsx`);
   *  - `null`: the row acted instead of completing (an action command fired its relay)
   *    and the composer clears;
   *  - `undefined`: the row no longer exists (the list moved under the pick); the menu
   *    goes and the draft is untouched. */
  onPick: (item: ComposerMenuItem) => string | null | undefined;
  /** Called after a pick that returned null, so the feature can clear the draft itself. */
  onClear?: () => void;
}

/**
 * Recall controller, injected by the feature layer: ArrowUp in an empty field lends the
 * field to something already written, edited in place like a shell's history. The
 * Composer owns the keys, banner and field; the feature owns what is recalled and what
 * saving means.
 *
 * While `draft()` is a string the field shows and edits it, leaving the operator's own
 * draft neither persisted over nor cleared. The feature can end the recall underneath
 * the Composer by answering `null`.
 */
export interface ComposerRecallApi {
  /** The recalled text being edited, or `null` when nothing is recalled. */
  draft: Accessor<string | null>;
  /** The banner text ("Editing queued message"), also announced when a recall begins. */
  label: string;
  /** Walk the recall: `older` from ArrowUp (empty field, or caret at the very start of a
   *  recalled one), `newer` from ArrowDown at its very end. Returns whether anything
   *  moved; stepping out past the newest is a move. */
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
   *  returns a promise: resolving `false` restores the text and attachments (the text
   *  only if the field is still empty, since the next message may be started). */
  onSend: (text: string, attachmentIds: string[]) => void | Promise<boolean>;
  /** Shift+Tab in the field. When given, the key is taken (backward focus traversal out
   *  of the field is given up; forward Tab still leaves); otherwise it is left to the
   *  browser. Never fires while the `/` or `@` menu is open. */
  onShiftTab?: () => void;
  /** The field's accessible name. Defaults to "Message". */
  label?: string;
  disabled?: boolean;
  /** Why this message cannot be sent, or null when it can. SEND is disabled and carries
   *  the reason on hover; the field stays live so the draft survives while the operator
   *  fixes the cause. Unlike `disabled` (a run owns the composer), this means the
   *  message would be refused, and it is actionable. */
  sendBlocked?: string | null;
  /** A run is generating: a STOP button wired to `onStop` joins SEND, and Escape in the
   *  field is a plain stop. When the field stays enabled (`disabled` false), SEND remains
   *  and the caller queues into the live run (steering); a disabled field shows STOP alone. */
  streaming?: boolean;
  /** Nothing is generating but something still runs that STOP ends (a thread's
   *  sub-agents): STOP joins SEND, which stays a plain send. */
  stoppable?: boolean;
  /** The run is waiting in line for capacity: the plate takes the queue tone, still. */
  queued?: boolean;
  /**
   * Invoked when STOP is pressed mid-stream (see `streaming`) with the field's text, so
   * stop and correction are one act (two controls leave a gap where the correction is
   * retyped or lost).
   *
   * The composer only hands over the text and clears the field; whether it becomes the
   * next turn is the caller's rule. `undefined` when the field was empty: a plain
   * interrupt, and the field takes focus.
   */
  onStop?: (correction?: string) => void;
  /** What SEND is called. Defaults to "Send". For the approval dock, which borrows the
   *  composer to collect a request for changes, where "Send" would misdescribe the act. */
  sendLabel?: string;
  /** Text to insert into the field programmatically (e.g. an undelivered queued message
   *  restored after a cancel). Applied whenever it becomes non-empty, appended below any
   *  current draft, then acknowledged via `onPrefillConsumed` so the caller can clear it. */
  prefill?: string | null;
  onPrefillConsumed?: () => void;
  placeholder?: string;
  /** `md` = docked input bar (default); `lg` = centered hero field. */
  size?: "md" | "lg";
  /** The plate's left label when `headerStart` gives none, e.g. the hero's "New thread · ready". */
  title?: string;
  /** The left of the plate: what the thread is doing (its name and `ready` at rest, the
   *  live run's activity while streaming). The caller's, since the design system can't
   *  reach run state.
   *
   *  One line of mono uppercase, dim at rest and info while `streaming`, unless a piece
   *  carries its own tone. While a message is recalled the Composer takes the whole line
   *  for the edit's label and keys. */
  headerStart?: JSX.Element;
  /** The right of the plate: the run's clock while streaming, the branch at rest, queued
   *  messages. When it renders nothing, a composer with a plate and a `storageKey` says
   *  `Draft saved` there while the field holds a persisted draft. */
  headerEnd?: JSX.Element;
  /** Something the thread is waiting on the operator for: the plate is replaced by the
   *  action tier's amber strip (`label`, e.g. "1 approval waiting") and `Jump ↑` takes
   *  them to the ask. Null when nothing is. */
  waiting?: { label: string; onJump?: () => void } | null;
  autofocus?: boolean;
  /** Persists the unsent draft to localStorage under this key, reactively; switching
   *  keys loads that key's draft. */
  storageKey?: string;
  /** Cells at the start of the status bar after `+ file`: settings describing what this
   *  message is (level, model). Each direct child is one `StatusBar` cell: a
   *  `StatusCell`, or a picker with `cell` set. */
  controls?: JSX.Element;
  /** Cells at the end of the status bar: the state of the thread the message is going
   *  into (tasks, stats, context fullness). A separate right-aligned slot so the bar
   *  reads "what this is, then where it's going" and keeps its edge when it wraps. */
  trailing?: JSX.Element;
  /** Drop the unit's fill but keep the frame, for a composer nested in another surface
   *  (the approval dock) where a fill on a fill is a box in a box. */
  bare?: boolean;
  /** File-attachment controller. When supplied, the Composer shows an attach
   *  button + drag-drop and renders the attachment chips; omit to hide them. */
  attachments?: ComposerAttachmentsApi;
  /** `/` and `@` menu controller. Omit and neither character is special (compare bench, approval dock). */
  menu?: ComposerMenuApi;
  /** ArrowUp-to-edit controller. Omit and the arrows are the field's own. */
  recall?: ComposerRecallApi;
  class?: string;
}
