/**
 * Whether the open thread has a live browser, and whether its panel has a reason to be
 * on screen.
 *
 * Read once when a conversation opens and then moved only by what the operator and the
 * stream report — there is no poll, because the backend says when a session ends on the
 * view stream the panel is already holding.
 *
 * **Two facts, because they come apart.** `active` is what the backend last said. The
 * panel stays offered after a session closes under the operator (`engaged`), so its
 * ended state and its "Open browser" button have somewhere to be drawn; ending the
 * session from the panel is the operator's own act and takes the panel with it.
 */

import { createEffect, createSignal, on, type Accessor } from "solid-js";
import { fetchBrowserSession } from "./data";

export interface BrowserState {
  /** The backend has a live session for this thread. */
  active: Accessor<boolean>;
  /** The panel has something to show: a live session, or one that just ended. */
  available: Accessor<boolean>;
  /** A session was started or attached from here. */
  opened: () => void;
  /** The backend reports no session, or the stream said it closed. The panel stays so
   *  it can say so. */
  gone: () => void;
  /** The operator ended the session; nothing is left to show. */
  ended: () => void;
}

export function createBrowserState(
  conversationId: () => string | null,
): BrowserState {
  const [active, setActive] = createSignal(false);
  const [engaged, setEngaged] = createSignal(false);

  createEffect(
    on(conversationId, (id, previous) => {
      // A new thread adopting its saved id is the same thread, and opening a browser
      // needs an id, so nothing is lost by leaving the signals alone.
      if (previous !== null && previous !== undefined) {
        setActive(false);
        setEngaged(false);
      }
      if (id === null) return;
      void fetchBrowserSession(id)
        .then((session) => {
          if (conversationId() === id && session.active) setActive(true);
        })
        .catch(() => {});
    }),
  );

  return {
    active,
    available: () => active() || engaged(),
    opened: () => {
      setActive(true);
      setEngaged(true);
    },
    gone: () => {
      setActive(false);
      setEngaged(true);
    },
    ended: () => {
      setActive(false);
      setEngaged(false);
    },
  };
}
