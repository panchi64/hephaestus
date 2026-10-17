import {
  For,
  Match,
  Show,
  Switch,
  createEffect,
  createMemo,
  type JSX,
} from "solid-js";
import {
  Button,
  EmptyState,
  ErrorBoundary,
  ErrorState,
  LoadingText,
} from "~/ui";
import type { HistoryStatus } from "../historyStatus";
import { consumeFoldAnchor, pendingFoldAnchor } from "../foldAnchor";
import type { ConversationActions } from "../conversationActions";
import type { createChatStream } from "../stream/chatStream";
import type { TranscriptFollow } from "../transcriptScroll";
import type { ChatViewport } from "../useChatViewport";
import { screenshotIndex } from "../screenshots";
import { TranscriptContexts } from "./TranscriptContexts";
import { InboxRows } from "./InboxRows";
import { MessageItem } from "./MessageItem";

export interface TranscriptViewProps {
  stream: ReturnType<typeof createChatStream>;
  viewport: ChatViewport;
  actions: ConversationActions;
  scroll: TranscriptFollow;
  conversationId: () => string | null;
  /** Whether the open thread's history is loading or failed — an empty transcript means
   *  "nothing said yet" only when it is neither. */
  history: () => HistoryStatus;
  /** Ask for the history again, from the error state's retry. */
  onRetryHistory: () => void;
  /** The reading measure, shared with the composer dock so the two agree. */
  measure: string;
  /** Height of what the room lays over the top of this scroll region (the title
   *  block), in px — the first turn starts beneath it and its fade. */
  insetTop: () => number;
}

/**
 * The scrolling conversation, and what each turn's controls are wired to.
 *
 * Every per-turn affordance the operator has — regenerate, edit, fork, rewind, pin,
 * reattach a dropped run — and the inbox rows queued behind the run resolve here, against the stream
 * for anything about a run and against the thread actions for anything about the
 * conversation. The room around it owns none of that; it owns where this sits.
 */
export function TranscriptView(props: TranscriptViewProps): JSX.Element {
  // Everything before the newest compaction turn is still in the transcript but
  // out of what the model replays. The transcript is the only place that knows a
  // turn's position relative to the fold, so the dim pass is derived here —
  // presentation only; the backend decided what it folded.
  //
  // A fold still *in flight* is skipped, and the guard is load-bearing: the live turn
  // sits at the tail, so counting it would dim the entire thread the moment a fold
  // starts — including the recent turns the fold is about to keep verbatim — and then
  // un-dim them when it lands. It has folded nothing yet.
  const screenshots = createMemo(() => screenshotIndex(props.stream.messages));

  const foldedThrough = createMemo(() => {
    let last = -1;
    props.stream.messages.forEach((m, i) => {
      if (m.role === "compaction" && !m.compaction) last = i;
    });
    return last;
  });

  // Take the operator to a fold that just settled. It is seated at the fold boundary
  // rather than where they were watching it happen, so on a long thread it arrives off
  // screen above — see `foldAnchor.ts`.
  //
  // The scroll goes through the follow (`scrollToMessage`) rather than straight to the
  // DOM, because the follow is pinned to the bottom and would otherwise put the operator
  // right back there on the next message change. The flash is the same "found it" marker
  // the design system already carries: scrolling alone puts the turn on screen without
  // saying which of them was the point.
  createEffect(() => {
    const id = pendingFoldAnchor();
    if (id === null) return;
    consumeFoldAnchor();
    // One frame later: the splice that seated the turn has to have rendered before
    // there is an element to find.
    requestAnimationFrame(() => {
      const el = props.scroll.scrollToMessage(id);
      if (!el) return;
      el.classList.add("heph-flash");
      setTimeout(() => el.classList.remove("heph-flash"), 3000);
    });
  });

  return (
    <TranscriptContexts
      sending={props.stream.sending}
      screenshots={screenshots}
    >
      <div class="relative flex min-h-0 flex-1 flex-col">
        <div
          ref={props.scroll.ref}
          tabindex={-1}
          onScroll={props.scroll.onScroll}
          /* `px-4` is the transcript's gutter inside the scroll container.

           The bright focus outline is gone — the shell's neutral focus halo
           covers this, and a white rule around the transcript was exactly
           the kind of border the system dropped.

           THE VERTICAL INSETS ARE THE OVERLAYS', and nothing more. Both ends of
           this region sit under something — the title laid over the top, the
           composer dock's fade reaching up over the bottom — and each inset is
           exactly what clears it: the title block's measured height plus one
           fade band at the top, one band at the bottom. The last turn's own
           `py-3` is the breathing room; a pad beyond the band would be a strip
           of bare page between the conversation and the composer. The scroll
           padding matches the top so a turn scrolled into view (a settled
           fold) lands below the title rather than under it. */
          class="min-h-0 flex-1 overflow-y-auto px-4 outline-none transition-colors"
          style={{
            "padding-top": `calc(${props.insetTop()}px + var(--scroll-fade-band))`,
            "scroll-padding-top": `calc(${props.insetTop()}px + var(--scroll-fade-band))`,
            "padding-bottom": "var(--scroll-fade-band)",
          }}
        >
          {/* The measure goes on the CONTENT, not on the scroll container:
            the container has to keep its full width so its scrollbar sits
            at the edge of the pane. */}
          <div ref={props.scroll.contentRef} class={props.measure}>
            {/* One malformed block must not cost the operator the composer,
              the thread list, or the text they were typing — scope a throw
              in the message tree to the scroll region. Switching threads
              resets it. */}
            <ErrorBoundary
              message="This conversation failed to render"
              resetKey={props.conversationId}
            >
              <Show
                when={props.stream.messages.length}
                fallback={
                  <Switch
                    fallback={
                      <EmptyState
                        icon="chat"
                        message="Start a conversation"
                        hint="Ask a question, request a summary, or describe a task to begin."
                      />
                    }
                  >
                    <Match when={props.history() === "loading"}>
                      <div class="px-4 py-8 text-center">
                        <LoadingText label="Loading conversation…" />
                      </div>
                    </Match>
                    <Match when={props.history() === "error"}>
                      <ErrorState
                        message="This conversation could not be loaded"
                        hint="Check the connection, then try again."
                        onRetry={props.onRetryHistory}
                      />
                    </Match>
                  </Switch>
                }
              >
                <For each={props.stream.messages}>
                  {(message, index) => (
                    <MessageItem
                      message={message}
                      dimmed={index() < foldedThrough()}
                      subagentCallsign={
                        message.role === "subagent"
                          ? props.viewport
                              .subagents()
                              .find(
                                (s) =>
                                  s.handle ===
                                  (message.author ?? message.reporter),
                              )?.callsign
                          : undefined
                      }
                      onResolveHostCommands={props.stream.resolveHostCommands}
                      onRegenerate={() =>
                        void props.stream.regenerate(message.id)
                      }
                      onContinue={() =>
                        void props.stream.continueTurn(message.id)
                      }
                      onCompactAndRetry={() =>
                        void props.stream.compactAndContinue(message.id)
                      }
                      compacting={props.stream.compacting()}
                      onEditMessage={(id, text) =>
                        void props.stream.edit(id, text)
                      }
                      onSwitchVersion={(id, i) =>
                        void props.stream.switchVersion(id, i)
                      }
                      onTogglePin={() =>
                        void props.stream.toggleMessagePin(message.id)
                      }
                      onOpenInView={props.viewport.openViewTo}
                      viewItems={props.viewport.items}
                      seenKey={() => props.viewport.state().seen.view ?? null}
                      onReattach={() => {
                        if (message.runId)
                          void props.stream.reattachRun(message.runId, {
                            fromSeq: props.stream.lastSeq(),
                          });
                      }}
                      onRewind={() => void props.stream.rewind(message.id)}
                      onFork={() => void props.actions.fork(message.id)}
                      onDelete={() =>
                        void props.actions.removeMessage(message.id)
                      }
                    />
                  )}
                </For>
              </Show>
              {/* Queued behind the running turn: dimmed, at the foot, where it will
                land. */}
              <InboxRows
                inbox={props.stream.inbox()}
                onEdit={(id, text) => void props.stream.editQueued(id, text)}
                onWithdraw={(id) => void props.stream.withdrawQueued(id)}
                onHold={(id, held) => void props.stream.holdQueued(id, held)}
              />
            </ErrorBoundary>
          </div>
        </div>
        <Show when={props.scroll.showJump()}>
          <Button
            variant="default"
            size="sm"
            leading="chevron-down"
            onClick={props.scroll.jumpToLatest}
            // `bottom-8` clears the dock's fade band, which reaches up over this
            // region's last 28px and would otherwise frost the button's lower half.
            class="absolute bottom-8 left-1/2 -translate-x-1/2 bg-surface"
          >
            Jump to latest
          </Button>
        </Show>
      </div>
    </TranscriptContexts>
  );
}
