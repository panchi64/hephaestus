import { createEffect, createSignal, type Accessor } from "solid-js";
import type { ComposerMenuApi } from "./types";
import type { ComposerMenuGroup, ComposerMenuItem } from "./ComposerMenu";
import { replaceToken, tokenAt, type ComposerToken } from "./composerToken";
import { cursorStep } from "../listCursor";
import { type Rect } from "../popoverPlacement";

export interface ComposerMenuControllerDeps {
  /** The feature's menu, or undefined when this Composer has none. */
  menu: () => ComposerMenuApi | undefined;
  /** The textarea, once mounted. */
  field: () => HTMLTextAreaElement | undefined;
  /** The operator's draft, and its setter — a pick completes the token inside it. */
  text: Accessor<string>;
  setText: (value: string) => void;
  /** True while the field shows something other than the draft (a recalled message):
   *  a `/` in it is text, not a command. */
  suspended: () => boolean;
}

export interface ComposerMenuController {
  open: () => boolean;
  groups: () => ComposerMenuGroup[];
  loading: () => boolean;
  anchor: Accessor<Rect | null>;
  activeId: Accessor<string | null>;
  setActiveId: (id: string) => void;
  /** Re-read the token under the caret — on input, caret moves and clicks. */
  sync: () => void;
  dismiss: () => void;
  pick: (item: ComposerMenuItem) => void;
  /** The navigation keys, taken before the Composer's own rules. Returns whether the
   *  key was consumed. */
  onKey: (e: KeyboardEvent, composing: boolean) => boolean;
}

/**
 * The Composer's `/` and `@` menu, as state: the token under the caret, the field's
 * rect to hang the panel off, and which row the keyboard is on. Three pieces and no
 * more — what the rows *are*, and what picking one means, is the feature's, through
 * `ComposerMenuApi`. Its own module so the Composer holds the field and the send, and
 * this holds the menu.
 */
export function createComposerMenuController(
  deps: ComposerMenuControllerDeps,
): ComposerMenuController {
  const [token, setToken] = createSignal<ComposerToken | null>(null);
  const [anchor, setAnchor] = createSignal<Rect | null>(null);
  const [activeId, setActiveId] = createSignal<string | null>(null);

  const groups = () => deps.menu()?.groups() ?? [];
  const rows = () => groups().flatMap((group) => group.items);
  const loading = () => deps.menu()?.loading?.() ?? false;
  // Open whenever a token is active, rows or not. A menu that silently never appears
  // leaves the operator unsure whether `/` did anything at all; one that says
  // "Loading…" and then "No matches" answers the question. The keys below still only
  // pick when there is a row to pick.
  const open = () => token() !== null;

  /** Re-read the token under the caret and tell the feature what to fetch.
   *
   *  Driven from input and from selection changes alike, because moving the caret back
   *  into a half-typed `/rev` is the same situation as having just typed it. */
  const sync = (): void => {
    const menu = deps.menu();
    const field = deps.field();
    if (!menu || !field || deps.suspended()) return;
    const next = tokenAt(field.value, field.selectionStart);
    const current = token();
    if (next?.trigger === current?.trigger && next?.query === current?.query) {
      // Same token, moved: keep the rows, re-measure in case the field grew a line.
      setToken(next);
      setAnchor(field.getBoundingClientRect());
      return;
    }
    setToken(next);
    setAnchor(next ? field.getBoundingClientRect() : null);
    menu.onQuery(next && { trigger: next.trigger, query: next.query });
  };

  // Keep the cursor on a row that still exists. A query narrowing under the operator's
  // fingers otherwise leaves the selection pointing at a row that has been filtered
  // away, and Enter would insert nothing.
  createEffect(() => {
    const ids = rows().map((row) => row.id);
    if (ids.length === 0) {
      setActiveId(null);
      return;
    }
    setActiveId((current) =>
      current !== null && ids.includes(current) ? current : ids[0]!,
    );
  });

  const dismiss = (): void => {
    setToken(null);
    setAnchor(null);
    deps.menu()?.onQuery(null);
  };

  const pick = (item: ComposerMenuItem): void => {
    const menu = deps.menu();
    const current = token();
    if (!menu || !current) return;
    const insert = menu.onPick(item);
    if (insert === undefined) {
      // The row was gone by the time it was picked — the list moved under the key.
      // Nothing happened, so nothing about the draft changes; only the menu goes.
      dismiss();
      return;
    }
    if (insert === null) {
      // The row acted rather than completed — an action command. The feature clears the
      // draft once its relay has fired; the composer only takes the menu down.
      deps.setText("");
      menu.onClear?.();
      dismiss();
      deps.field()?.focus();
      return;
    }
    const next = replaceToken(deps.text(), current, insert);
    deps.setText(next.text);
    dismiss();
    // After the DOM has the new value, or the caret lands at the old length.
    queueMicrotask(() => {
      const field = deps.field();
      field?.focus();
      field?.setSelectionRange(next.caret, next.caret);
    });
  };

  /** Handled here rather than on the panel: focus never leaves the textarea while the
   *  menu is up, and the panel is portalled to `document.body`, so nothing typed in the
   *  field would ever reach a handler over there. */
  const onKey = (e: KeyboardEvent, composing: boolean): boolean => {
    if (!open()) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const ids = rows().map((row) => row.id);
      const next = cursorStep(e.key, ids.indexOf(activeId() ?? ""), ids.length);
      // Taken even with no rows: the arrows belong to the menu while it is up, and
      // letting one through would jump the caret out of the token it is showing.
      e.preventDefault();
      if (next !== null) setActiveId(ids[next]!);
      return true;
    }
    // A pick, not a send — a composing IME's Enter is the IME's.
    if ((e.key === "Enter" || e.key === "Tab") && !composing) {
      const row = rows().find((item) => item.id === activeId());
      if (!row) return false;
      e.preventDefault();
      pick(row);
      return true;
    }
    if (e.key === "Escape") {
      // Stopped as well as prevented: the menu is the innermost thing Escape can mean,
      // and letting it through would also close the panel or the room behind it.
      e.preventDefault();
      e.stopPropagation();
      dismiss();
      return true;
    }
    return false;
  };

  return {
    open,
    groups,
    loading,
    anchor,
    activeId,
    setActiveId,
    sync,
    dismiss,
    pick,
    onKey,
  };
}
