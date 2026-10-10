import type { Accessor } from "solid-js";
import type { ComposerMenuController } from "./composerMenuController";
import type { ComposerProps } from "./types";

/** An IME is mid-composition: its Enter commits a candidate, and acting on it would
 *  send (or pick) half a word. `keyCode` 229 is the only signal some engines give. */
const composing = (e: KeyboardEvent): boolean =>
  e.isComposing || e.keyCode === 229;

/**
 * The field's keyboard rules, in the order they are taken: the `/` `@` menu, then the
 * recall's keys, then the Composer's own (send, Escape, Shift+Tab). Handlers only — the
 * field binds them; nothing here listens.
 */
export function createComposerKeys(deps: {
  props: ComposerProps;
  menuCtl: ComposerMenuController;
  field: () => HTMLTextAreaElement | undefined;
  text: Accessor<string>;
  recalling: Accessor<boolean>;
  recalled: Accessor<string | null>;
  awaiting: Accessor<boolean>;
  submit: () => void;
  cancelAwaiting: () => void;
  announce: (message: string) => void;
}): { onKeyDown: (e: KeyboardEvent) => void; saveRecall: () => void } {
  const { props, menuCtl, field, text, recalling, recalled, awaiting } = deps;

  /** Commit a recalled edit. An emptied one is not a save — SAVE is disabled over it —
   *  since taking a message back is a withdraw, and that is not what this key says. */
  const saveRecall = (): void => {
    if (!recalled()?.trim()) return;
    props.recall?.save();
    deps.announce("Saved");
    field()?.focus();
  };

  /** Walk the recall and land the caret at the end of whatever the field now shows —
   *  after the DOM has the new value, or it lands at the old length. */
  const stepRecall = (direction: "older" | "newer"): void => {
    if (!props.recall?.step(direction)) return;
    queueMicrotask(() => {
      const end = field()?.value.length ?? 0;
      field()?.setSelectionRange(end, end);
    });
  };

  /** The recall's keys, taken after the menu's and before the Composer's own rules — so
   *  Escape here cancels the edit rather than stopping the run, and ⌘/Ctrl+Enter saves
   *  it rather than sending. The arrows only fire at the field's edges: ArrowUp on an
   *  empty field or with the caret at the very start, ArrowDown at the very end. Anywhere
   *  else they move the caret through a multi-line message as they always have. */
  const recallKey = (e: KeyboardEvent): boolean => {
    const recall = props.recall;
    const el = field();
    if (!recall || !el || composing(e)) return false;
    const plain = !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey;
    const { selectionStart: start, selectionEnd: end } = el;
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
      const length = el.value.length;
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
      el.focus();
      return true;
    }
    return false;
  };

  const onKeyDown = (e: KeyboardEvent): void => {
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
      if (!awaiting()) deps.submit();
      return;
    }
    if (e.key === "Escape") {
      // Innermost first: a held send, then the run. Stopped from propagating either
      // way, because Escape here meant this and not the panel behind the composer.
      if (awaiting()) {
        e.preventDefault();
        e.stopPropagation();
        deps.cancelAwaiting();
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

  return { onKeyDown, saveRecall };
}
