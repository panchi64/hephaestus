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
 * The scrolling conversation. Per-turn controls and the queued inbox rows are wired here:
 * to the stream for anything about a run, to the thread actions for anything about the
 * conversation.
 */
export function TranscriptView(props: TranscriptViewProps): JSX.Element {
  const screenshots = createMemo(() => screenshotIndex(props.stream.messages));

  // Turns before the newest compaction turn are dimmed (still shown, no longer replayed).
  // A fold still in flight is skipped: the live turn sits at the tail, so counting it
  // would dim the whole thread, including turns the fold keeps verbatim.
  const foldedThrough = createMemo(() => {
    let last = -1;
    props.stream.messages.forEach((m, i) => {
      if (m.role === "compaction" && !m.compaction) last = i;
    });
    return last;
  });

  // Scrolls to a fold that just settled; it is seated at the fold boundary, off screen on
  // a long thread (see `foldAnchor.ts`). Goes through the follow, since a pinned follow
  // would return to the bottom on the next growth. The flash marks which turn was the point.
  createEffect(() => {
    const id = pendingFoldAnchor();
    if (id === null) return;
    consumeFoldAnchor();
    // One frame later: the splice that seated the turn has to render first.
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
          /* The vertical insets are exactly what clears the overlays: the title block's
           measured height plus one fade band at the top, one band at the bottom. A larger
           pad would leave bare page above the composer. Scroll padding matches the top so
           a scrolled-to turn lands below the title.

           Native scroll anchoring is off: `transcriptScroll.ts` keeps its own anchor and
           two would double the correction. */
          class="min-h-0 flex-1 overflow-y-auto px-4 outline-none transition-colors [overflow-anchor:none]"
          style={{
            "padding-top": `calc(${props.insetTop()}px + var(--scroll-fade-band))`,
            "scroll-padding-top": `calc(${props.insetTop()}px + var(--scroll-fade-band))`,
            "padding-bottom": "var(--scroll-fade-band)",
          }}
        >
          {/* The measure goes on the content so the scrollbar stays at the pane edge. The
            room a send reserves sits on this wrapper, not on the observed content. */}
          <div
            class={props.measure}
            style={{
              "min-height":
                props.scroll.room() === null
                  ? undefined
                  : `${props.scroll.room()}px`,
            }}
          >
            <div ref={props.scroll.contentRef}>
              {/* Scopes a throw in the message tree to this region so the composer and
              typed text survive. Switching threads resets it. */}
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
                          props.stream.edit(id, text)
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
                <InboxRows
                  inbox={props.stream.inbox()}
                  onEdit={(id, text) => void props.stream.editQueued(id, text)}
                  onWithdraw={(id) => void props.stream.withdrawQueued(id)}
                  onHold={(id, held) => void props.stream.holdQueued(id, held)}
                />
              </ErrorBoundary>
              <div ref={props.scroll.endRef} aria-hidden="true" />
            </div>
          </div>
        </div>
        <Show when={props.scroll.showJump()}>
          <Button
            variant="default"
            size="sm"
            leading="chevron-down"
            onClick={props.scroll.followLatest}
            // `bottom-8` clears the dock's fade band over this region's last 28px.
            class="absolute bottom-8 left-1/2 -translate-x-1/2 bg-surface"
          >
            {props.scroll.newBelow() ? "New text below" : "Jump to latest"}
          </Button>
        </Show>
      </div>
    </TranscriptContexts>
  );
}
