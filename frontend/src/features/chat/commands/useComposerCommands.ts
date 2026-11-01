/**
 * Binding the `/` menu to the room.
 *
 * The Composer owns the token and the keys; this owns the rows and what picking one
 * means. Two kinds of meaning, and the split is the backend's, not ours:
 *
 * - A **prompt** command completes the token in the field. Nothing else happens until
 *   the operator sends, and what rides that send is `{name, argument}` — never an
 *   expansion. The backend decides what the command meant.
 * - An **action** command fires a relay the room already owns and sends no message. The
 *   catalog names a stable `actionId`; this maps it onto the handler the header menu and
 *   the rail already call. **An id this build does not know renders disabled** rather
 *   than throwing, which is what lets the backend add one without waiting for a client.
 *
 * Nothing here decides *whether* a command is available — the route already dropped what
 * this thread cannot honour.
 */

import { createMemo, createSignal } from "solid-js";
import type {
  ComposerMenuGroup,
  ComposerMenuItem,
  ComposerTrigger,
} from "~/ui";
import { settled } from "~/lib/resource";
import { useCommands } from "./data";
import type { Command, CommandActionId, CommandInvocation } from "./model";
import {
  groupCommands,
  invocationName,
  rankCommands,
  resolveTyped,
} from "./rank";

/** The relays the room already has. Each is the *same* handler its existing control
 *  calls — a command is a second way to reach one, never a second implementation. */
export interface CommandActions {
  compact: (focus?: string) => void;
  fork: () => void;
  retitle: () => void;
  newThread: () => void;
  setPermissionLevel: (level: string) => void;
}

/** What a send turns out to be, once the staged command is read against the text.
 *
 *  Three outcomes rather than two, because `/level auto` is an action whose argument the
 *  operator types *after* picking it — so it cannot fire on the pick like `/compact`
 *  does, and it must not become a message either. A send that resolves to `acted` is
 *  swallowed: the relay has run and there is nothing to say to the model. */
export type SendIntent =
  { kind: "message"; command: CommandInvocation | null } | { kind: "acted" };

export interface ComposerCommands {
  /** Rows for the Composer's menu, already ranked and grouped. */
  groups: () => ComposerMenuGroup[];
  /** The Composer reports the token here; `null` closes the menu. */
  onQuery: (token: { trigger: ComposerTrigger; query: string } | null) => void;
  /** Completion text for a command, null when the row acted immediately, or undefined
   *  when the row is no longer in the list (the catalog refetched under the menu) — the
   *  Composer then only dismisses, rather than clearing a draft nothing acted on. */
  onPick: (item: ComposerMenuItem) => string | null | undefined;
  /** The command a draft names by typing it rather than picking it — an exact `/name`
   *  at its head — resolved to what a pick would have staged, or null. */
  resolveTyped: (text: string) => { command: Command; name: string } | null;
  /** Read the staged command against what is about to be sent, and say what this send
   *  actually is. Nothing staged by a pick falls back to `resolveTyped`, so a command
   *  the operator typed in full means the same as one they picked. Clears the staging
   *  either way. */
  consume: (text: string) => SendIntent;
  /** Drop a staged command — the operator edited the token away. */
  clear: () => void;
}

/** Row id: the qualified name, which is unique across the whole catalog by
 *  construction. `aria-activedescendant` points at it, so it has to be unique across
 *  *groups*, not merely within one. */
const rowId = (command: Command): string => `cmd-${command.qualifiedName}`;

/** `text` begins with `token` and ends it there — not part of a longer name. */
const opensWith = (text: string, token: string): boolean =>
  text.startsWith(token) &&
  (text.length === token.length || /\s/.test(text[token.length]!));

function toItem(command: Command): ComposerMenuItem {
  return {
    id: rowId(command),
    label: command.name,
    detail: command.description,
    // A shadowed row says which name it lost to, rather than looking like a duplicate.
    meta: command.shadowedBy
      ? `also ${command.shadowedBy}`
      : (command.argumentHint ?? undefined),
  };
}

export function createComposerCommands(
  conversationId: () => string | null,
  projectId: () => string | null,
  actions: CommandActions,
): ComposerCommands {
  const catalog = useCommands(conversationId, projectId);
  const [query, setQuery] = createSignal<string | null>(null);
  // The picked command, held until the send reads it back against the typed text. The
  // whole spec, not just its name: the send has to know whether this was a message or
  // an action, and re-finding it in a catalog that may have refetched is a lookup that
  // can fail for no reason the operator caused.
  const [pending, setPending] = createSignal<{
    command: Command;
    name: string;
  } | null>(null);

  const matched = createMemo(() => {
    const q = query();
    // `settled`, not a bare `.latest`: the menu opens on a keystroke and must not suspend
    // the room behind it while the catalog is in flight. `latest` alone calls through on
    // an unresolved resource, which in this tracked scope registers with the nearest
    // `Suspense` and takes the panel off screen instead of rendering it.
    const loaded = settled(catalog);
    if (q === null || !loaded) return null;
    return {
      commands: rankCommands(q, loaded.commands),
      groups: loaded.groups,
    };
  });

  const groups = (): ComposerMenuGroup[] => {
    const hit = matched();
    if (!hit) return [];
    return groupCommands(hit.commands, hit.groups).map((group) => ({
      id: group.id,
      label: group.label,
      items: group.commands.map(toItem),
    }));
  };

  const find = (id: string): Command | undefined =>
    matched()?.commands.find((command) => rowId(command) === id);

  /** The relay for an action that runs the moment it is picked, or undefined when there
   *  is none — either because the id needs an argument, or because this build has never
   *  heard of it. An unknown id does nothing rather than throwing: the catalog is the
   *  backend's, and it may name an action a client has not shipped support for yet. */
  const relay = (id: CommandActionId | null): (() => void) | undefined => {
    switch (id) {
      case "compact":
        return () => actions.compact();
      case "fork":
        return actions.fork;
      case "retitle":
        return actions.retitle;
      case "new-thread":
        return actions.newThread;
      default:
        return undefined;
    }
  };

  /** Against the whole catalog, not the menu's ranked rows: a typed command is sent
   *  with the menu shut, and a name the menu is not showing is still a name. */
  const typed = (text: string) => {
    const loaded = settled(catalog);
    return loaded ? resolveTyped(text, loaded.commands) : null;
  };

  return {
    groups,
    onQuery: (token) => {
      // `@` is the file picker's trigger, not this one's. Reporting null rather than
      // ignoring it keeps one menu open at a time.
      setQuery(token?.trigger === "/" ? token.query : null);
    },
    resolveTyped: typed,
    onPick: (item) => {
      const command = find(item.id);
      // Undefined, not null: null means "the row acted", and the Composer clears the
      // field on it. A row that vanished under the pointer did nothing, and the draft
      // it was about to complete is still the operator's.
      if (!command) return undefined;
      const name = invocationName(command);
      if (command.kind === "action") {
        const run = relay(command.actionId);
        if (run) {
          // Runs now and clears the field — there is no message to write.
          run();
          return null;
        }
        // An action carrying an argument (the permission level) completes the token
        // instead; the operator types the level and the send fires it. Staged, so the
        // send knows this is not a message.
        setPending({ command, name });
        return name;
      }
      setPending({ command, name });
      return name;
    },
    consume: (text) => {
      const picked = pending();
      setPending(null);
      const typedText = text.trimStart();
      // The operator may have edited the token away between picking and sending, and
      // the **text is the turn of record** — so a staged command only counts while the
      // message still names it. When it doesn't, the text is read on its own terms: a
      // pick later retyped as a different command is that other command.
      //
      // As a **whole token**, not a prefix: `/review` is the head of `/reviewer`, and a
      // bare `startsWith` would both keep the wrong command and slice its argument at the
      // wrong place, sending `reviewer the auth` as `review` carrying `er the auth`. The
      // backend applies the same rule when carrying a command through an edit
      // (`routes/chat._opens_with`), and `resolveTyped` applies it too.
      const staged =
        picked && opensWith(typedText, `/${picked.name}`)
          ? picked
          : typed(text);
      if (!staged) return { kind: "message", command: null };
      const argument = typedText.slice(staged.name.length + 1).trim();
      if (staged.command.kind === "action") {
        if (staged.command.actionId === "permission-level" && argument) {
          actions.setPermissionLevel(argument);
        } else if (staged.command.actionId === "compact") {
          // Only a *typed* `/compact` reaches here — a picked one ran on the pick and was
          // never staged. Text after it is what the summary should keep.
          actions.compact(argument || undefined);
        } else {
          relay(staged.command.actionId)?.();
        }
        // Swallowed either way: an action is never a message, and one sent without its
        // argument has nothing to do — the field clears and the operator tries again.
        return { kind: "acted" };
      }
      return { kind: "message", command: { name: staged.name, argument } };
    },
    clear: () => setPending(null),
  };
}
