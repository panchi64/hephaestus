/**
 * Binding the `@` menu to the room.
 *
 * Simpler than its `/` counterpart, because there is only one kind of row: picking a file
 * completes the token and stages a reference that rides the next send. Nothing acts.
 *
 * **Only where there is a filesystem to browse.** A thread with no project works in a
 * container with nothing in it the operator has ever seen, so `@` means nothing there and
 * the menu stays shut rather than opening empty.
 */

import { createSignal } from "solid-js";
import type {
  ComposerMenuGroup,
  ComposerMenuItem,
  ComposerTrigger,
} from "~/ui";
import { settled } from "~/lib/resource";
import { useProjectFiles } from "./data";

export interface ComposerFileRefs {
  groups: () => ComposerMenuGroup[];
  onQuery: (token: { trigger: ComposerTrigger; query: string } | null) => void;
  /** The path to complete the token with, or undefined when the row is no longer in the
   *  listing. Never null: that means "the row acted" to the Composer, which clears the
   *  field on it, and no file row ever acts. */
  onPick: (item: ComposerMenuItem) => string | undefined;
  /** The references still named by `text`, for the send. Clears the staging. */
  consume: (text: string) => string[];
  clear: () => void;
}

/** Row id: the path, which is unique within a listing by construction. */
const rowId = (path: string): string => `file-${path}`;

/** The run of characters after a token that could still be part of the same path. */
const PATH_RUN = /^[^\s"'`,;:)\]}>]*/;

/** `text` names `@path` as a whole token rather than as the head of a longer one.
 *
 *  A plain `includes` is not this rule: `@src/a.ts` is a substring of `@src/a.tsx`, so a
 *  reference the operator has typed *past* would ride the send naming a file the message
 *  does not. The backend applies the same rule when carrying references through an edit
 *  (`routes/chat._names_path`), and the two have to agree — they are the same question
 *  asked at two moments.
 *
 *  **A trailing dot is punctuation, not an extension.** "please read @src/a.ts." is an
 *  ordinary sentence and the commonest way a path is written in one, so a rule that only
 *  asked "is the next character path-shaped" would drop the reference on a full stop while
 *  correctly dropping it on `.bak`. The two are told apart by what comes *after* the dots:
 *  nothing more of the path means the sentence ended. */
export function namesPath(text: string, path: string): boolean {
  const token = `@${path}`;
  for (
    let start = text.indexOf(token);
    start !== -1;
    start = text.indexOf(token, start + 1)
  ) {
    const run = PATH_RUN.exec(text.slice(start + token.length))![0];
    if (run === "" || /^\.+$/.test(run)) return true;
  }
  return false;
}

/** The path behind a picked row, or undefined when the listing no longer holds it — a
 *  refetch between the menu drawing and the click. */
export function pickedPath(
  entries: readonly { path: string }[],
  itemId: string,
): string | undefined {
  return entries.find((file) => rowId(file.path) === itemId)?.path;
}

export function createComposerFileRefs(
  projectId: () => string | null,
  conversationId: () => string | null,
): ComposerFileRefs {
  const [query, setQuery] = createSignal<string | null>(null);
  const [staged, setStaged] = createSignal<string[]>([]);
  const files = useProjectFiles(projectId, conversationId, query);

  const groups = (): ComposerMenuGroup[] => {
    // `settled`, not a bare `.latest`: the menu opens on a keystroke and must not suspend
    // the room behind it while the listing is in flight. `latest` alone is not that read —
    // on an *unresolved* resource it calls through, which inside this tracked scope
    // registers with the nearest `Suspense` and takes the panel off screen rather than
    // rendering it. `settled` guards on `state` first, so a fetch in flight reads
    // `undefined` and the menu simply stays shut until the rows arrive.
    const listing = settled(files);
    if (query() === null || !listing || listing.entries.length === 0) return [];
    return [
      {
        id: "files",
        // The label says which tree answered, because the two genuinely differ: a
        // worktree is cut from the project's base ref, so the operator's uncommitted
        // work is in one and not the other.
        label: listing.root === "worktree" ? "Files in this branch" : "Files",
        items: listing.entries.map((file): ComposerMenuItem => ({
          id: rowId(file.path),
          label: file.name,
          // The path under the name, not instead of it: the operator is looking for a
          // file and then checking which one, in that order.
          detail: file.path,
        })),
      },
    ];
  };

  return {
    groups,
    onQuery: (token) => {
      // `/` is the command menu's trigger, not this one's. Reporting null rather than
      // ignoring keeps one menu open at a time.
      setQuery(token?.trigger === "@" ? token.query : null);
    },
    onPick: (item) => {
      const path = pickedPath(settled(files)?.entries ?? [], item.id);
      if (!path) return undefined;
      setStaged((current) =>
        current.includes(path) ? current : [...current, path],
      );
      return path;
    },
    consume: (text) => {
      const all = staged();
      setStaged([]);
      // The **text is the turn of record**, so a reference only counts while the message
      // still names it — the operator may have picked a file and then deleted the token.
      return all.filter((path) => namesPath(text, path));
    },
    clear: () => setStaged([]),
  };
}
