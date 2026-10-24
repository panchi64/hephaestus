/**
 * A conversation's inbox as the client holds it: what waits for the model, in delivery
 * order, and whether the operator's Stop is holding all of it.
 *
 * Built from the `GET` read (on load, after a send, on run end and after a resume) and
 * the run's `message.*` / `inbox.waiting` events. Messages queued while no run is live
 * emit nothing, hence the read. Every transition is idempotent so a replay and a read
 * can overlap.
 */

import type { RunEvent } from "~/lib/stream";
import type { InboxDTO, InboxMessageDTO } from "./data/wire";

export interface InboxItem {
  id: string;
  text: string;
  source: "operator" | "subagent" | "agent" | "system";
  /** The agent or sub-agent it came from, when it has one. */
  sender: string | null;
  held: boolean;
  /** Opens a turn of its own after the current one instead of joining it. */
  turn: boolean;
  /** For a system item, which notice it is. */
  notice: string | null;
  queuedAt: string;
}

export interface InboxState {
  items: InboxItem[];
  /** The operator stopped a turn; nothing starts until they act again. */
  waiting: boolean;
  /** Deliveries wait for the operator's next turn: the delegation tree took its
   *  unattended turns, or no turn could be opened on the thread. */
  agentsPaused: boolean;
}

export const emptyInbox: InboxState = {
  items: [],
  waiting: false,
  agentsPaused: false,
};

const toItem = (m: InboxMessageDTO): InboxItem => ({
  id: m.id,
  text: m.text,
  source: m.source,
  sender: m.sender,
  held: m.held,
  turn: m.turn,
  notice: m.notice,
  queuedAt: m.queued_at,
});

export function inboxFromDto(dto: InboxDTO): InboxState {
  return {
    items: dto.messages.map(toItem),
    waiting: dto.waiting_for_operator,
    agentsPaused: dto.agents_paused,
  };
}

/** The operator's own, which is the only kind they can edit, hold or withdraw. */
export const isOperatorItem = (item: InboxItem): boolean =>
  item.source === "operator";

/** What a waiting item is, in the words its row reads. The operator's own show their
 *  text instead, so this is only asked of the rest. */
export function waitingLabel(item: InboxItem): string {
  switch (item.source) {
    case "subagent":
      return `Report from ${item.sender ?? "a sub-agent"}`;
    case "agent":
      return `Message from ${item.sender ?? "an agent"}`;
    case "system": {
      const notice = (item.notice ?? "notice").replace(/[-_]+/g, " ");
      return notice.charAt(0).toUpperCase() + notice.slice(1);
    }
    case "operator":
      return "Your message";
  }
}

/** Why a waiting item is not on its way, or null when it is. Held is the item's own;
 *  the other two are the inbox's, and paused agents stop only what an agent sent. */
export function idleReason(state: InboxState, item: InboxItem): string | null {
  if (item.held) return "Held";
  if (state.waiting) return "Waiting for you";
  if (state.agentsPaused && !isOperatorItem(item)) return "Sub-agents paused";
  return null;
}

/** Whether a turn for something queued is on its way: an item nothing holds back, which
 *  the backend starts a turn for once the thread is idle. */
export const deliveryDue = (state: InboxState): boolean =>
  state.items.some((item) => idleReason(state, item) === null);

const patchItem = (
  state: InboxState,
  id: string,
  patch: Partial<InboxItem>,
): InboxState =>
  state.items.some((i) => i.id === id)
    ? {
        ...state,
        items: state.items.map((i) => (i.id === id ? { ...i, ...patch } : i)),
      }
    : state;

export function removeItem(state: InboxState, id: string): InboxState {
  return state.items.some((i) => i.id === id)
    ? { ...state, items: state.items.filter((i) => i.id !== id) }
    : state;
}

export function editItem(
  state: InboxState,
  id: string,
  text: string,
): InboxState {
  return patchItem(state, id, { text });
}

export function holdItem(
  state: InboxState,
  id: string,
  held: boolean,
): InboxState {
  return patchItem(state, id, { held });
}

/** Fold one run event into the inbox; the state itself when the event is not about it. */
export function foldInboxEvent(state: InboxState, ev: RunEvent): InboxState {
  switch (ev.type) {
    case "message.queued":
      if (state.items.some((i) => i.id === ev.message_id)) return state;
      return {
        ...state,
        items: [
          ...state.items,
          {
            id: ev.message_id,
            text: ev.text,
            source: ev.source ?? "operator",
            sender: ev.sender ?? null,
            held: false,
            turn: ev.turn ?? false,
            notice: ev.notice ?? null,
            queuedAt: ev.ts,
          },
        ],
      };
    case "message.edited":
      return editItem(state, ev.message_id, ev.text);
    case "message.held":
      return holdItem(state, ev.message_id, ev.held);
    case "message.withdrawn":
    case "message.injected":
      return removeItem(state, ev.message_id);
    case "inbox.waiting":
      return state.waiting === ev.waiting
        ? state
        : { ...state, waiting: ev.waiting };
    default:
      return state;
  }
}
