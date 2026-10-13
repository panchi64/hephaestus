/**
 * Which workspace sections of the rail the operator has opened or closed.
 *
 * Modelled on the pin store beside it, and held for the same reason: this is the
 * operator's own arrangement of the list, not something the backend has an opinion
 * about, and a component that remembered it per mount would forget it on every reload.
 *
 * **Three-valued on purpose.** `undefined` means the operator has never touched this
 * section, which is different from having closed it — an untouched section is free to
 * open itself when it holds the thread you are reading, and a closed one is not. Storing
 * only "the open ones" would collapse those two into one and make an explicit close
 * indistinguishable from a default.
 *
 * Keyed by project id rather than by name: two directories can both be called
 * `frontend`, and one remembered state between them would toggle the wrong section.
 */

import { createSignal } from "solid-js";
import { readLS, writeLS } from "~/lib/storage";

const KEY = "heph.chat.workspaces";

function read(): Record<string, boolean> {
  try {
    const raw = readLS(KEY);
    // A successful parse is not the same as a usable one: `null`, a number and an
    // array all parse fine and would then be indexed as the store's state — and the
    // first read throws while rendering a heading, on a rail mounted by every route.
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, boolean>)
      : {};
  } catch {
    return {};
  }
}

const [state, setState] = createSignal<Record<string, boolean>>(read());

/** The operator's explicit choice for this section, or `undefined` if they have made
 *  none — in which case the caller's own derivation decides. */
export function workspaceOpen(projectId: string): boolean | undefined {
  return state()[projectId];
}

/** Record that this section is now open or closed. Always writes a value: once the
 *  operator has moved a section by hand, it stays where they put it. */
export function setWorkspaceOpen(projectId: string, open: boolean): void {
  const next = { ...state(), [projectId]: open };
  setState(next);
  writeLS(KEY, JSON.stringify(next));
}
