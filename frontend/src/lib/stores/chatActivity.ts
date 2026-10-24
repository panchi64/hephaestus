/** Global chat-activity echoes for the main chat room.
 *
 *  `chatBusy` — true while a turn is streaming. The app shell's nav rail reads it to
 *  light an ambient indicator on the Chat item, so a run started before navigating away
 *  (to Email, Memory, …) is visibly still working from anywhere — returning to the room
 *  then feels continuous, not like a response that was abandoned.
 *
 *  `runErrored` — true when the room's last run ended in `run.error`; cleared when the
 *  next run starts. Feeds the platform-status derivation that tints the favicon red.
 *
 *  `awaitingInput` — true while the room has a live, unanswered park: a sensitive tool
 *  call awaiting the operator's decision, or a question awaiting their answer. Cleared the
 *  moment it's settled (approved/denied/answered/decided elsewhere) or the run stops being
 *  in flight (e.g. a cancel). Distinct from `chatBusy`: this is a stronger "needs YOU,
 *  specifically" signal, not just ambient activity — the nav rail and favicon both give it
 *  its own (warn) tone rather than folding it into the plain busy indicator.
 *
 *  One signal for both reasons, because what it drives — an attention tint elsewhere in
 *  the app — is the same in both cases: the run has stopped and will not go on until the
 *  operator comes back to it.
 *
 *  `chatThreadId` — the thread open in the room, so the ambient field can tell the open
 *  thread's own events from the thread list's.
 *
 *  All of them are presentation echoes only: the backend owns the run; the chat seam
 *  mirrors its state here and the consumers just render. Driven solely by the main
 *  `mainChat()` room (not the ephemeral compare panes), so they track the one
 *  conversation the operator would return to. */
import { createSignal } from "solid-js";

const [chatBusy, setChatBusy] = createSignal(false);
const [runErrored, setRunErrored] = createSignal(false);
const [awaitingInput, setAwaitingInput] = createSignal(false);
const [chatThreadId, setChatThreadId] = createSignal<string | null>(null);

export {
  chatThreadId,
  setChatThreadId,
  chatBusy,
  setChatBusy,
  runErrored,
  setRunErrored,
  awaitingInput,
  setAwaitingInput,
};
