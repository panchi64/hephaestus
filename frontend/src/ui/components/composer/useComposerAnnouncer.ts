import { createEffect, createSignal, untrack, type Accessor } from "solid-js";
import { SEND_CHORD } from "./sendChord";
import type { ComposerProps } from "./types";

/**
 * Screen-reader announcements: one polite live region for the state changes that
 * otherwise only show up as a label swap somewhere the operator isn't looking.
 * Cleared and re-set on the next tick so the same sentence twice is announced twice.
 */
export function createComposerAnnouncer(
  props: ComposerProps,
  recalling: Accessor<boolean>,
): { announcement: Accessor<string>; announce: (message: string) => void } {
  const [announcement, setAnnouncement] = createSignal("");
  const announce = (message: string): void => {
    setAnnouncement("");
    queueMicrotask(() => setAnnouncement(message));
  };
  let wasStreaming = untrack(() => props.streaming) ?? false;
  createEffect(() => {
    const streaming = props.streaming ?? false;
    if (streaming && !wasStreaming)
      announce(
        untrack(() => props.disabled)
          ? "Agent running"
          : `Agent running — ${SEND_CHORD} queues, Esc stops`,
      );
    wasStreaming = streaming;
  });
  createEffect(() => {
    const reason = props.sendBlocked;
    if (reason) announce(reason);
  });
  // The banner says it to a sighted operator; this says it to everyone else. On the
  // entering edge only — walking from one recalled message to the next is the same mode.
  let wasRecalling = false;
  createEffect(() => {
    const now = recalling();
    if (now && !wasRecalling)
      announce(untrack(() => props.recall?.label) ?? "Editing");
    wasRecalling = now;
  });
  return { announcement, announce };
}
