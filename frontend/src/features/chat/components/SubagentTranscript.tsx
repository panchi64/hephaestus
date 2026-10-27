import { For, Match, Show, Switch, createMemo, type JSX } from "solid-js";
import { LoadingText, Text } from "~/ui";
import { isLive, type Subagent } from "../data";
import { useSubagentTranscript } from "../subagentTranscript";
import { TurnBlocks } from "./TurnBlocks";

/**
 * What a sub-agent actually did — the body of its own view (`SubagentDetail`).
 *
 * **It is a conversation, so it is read as one.** `useChatSession` is the main room's
 * own loader and `TurnBlocks` its own renderer — the think → tool → text sequence, the
 * work log, the folding, all of it — so a sub-agent's transcript reads exactly like the
 * agent's own work, which is what it is. Writing a second, simpler renderer here would
 * have meant a transcript that quietly showed less than the real one.
 *
 * **`TurnBlocks`, not `MessageItem`.** That wrapper carries the main room's actions —
 * edit, fork, rewind, retry — every one of which acts on a thread the operator is in.
 * Nobody can type into a sub-agent, so those are not merely unused here, they are
 * offers that could not be honoured.
 *
 * **The opening prompt is shown, and it is not the operator's.** It is the task the
 * agent wrote, which is the first thing worth reading: a sub-agent that answered the
 * wrong question usually answered the question it was actually given.
 *
 * **A live tail only once opened as a thread.** Until then a working card shows the
 * transcript as of the last poll, with a line saying so: a pane holding several would
 * otherwise hold several open connections to watch work nobody is blocked on. Opening
 * one as a thread is the operator choosing to follow it, so from then on it follows the
 * run's own stream (`useSubagentTranscript`).
 *
 * **What re-reads a polled one.** The card's own figures: `contextUsed` only moves when
 * the sub-agent has made another model request, so the read re-fetches when there is new
 * transcript to see and never on a poll that found nothing new. A finished sub-agent's
 * figures stop moving, so its transcript is read once and then left alone.
 *
 * **No inset of its own.** It used to sit under a card with a rule down its left edge
 * saying "this belongs to that". It is now the pane's whole content, and a border
 * marking a relationship to something no longer on screen is a line with nothing to say.
 */
export function SubagentTranscript(props: { subagent: Subagent }): JSX.Element {
  /* Two arms rather than one body reading `opened`: the source is chosen at mount, so
     opening the sub-agent as a thread while its transcript is on screen remounts it
     once into the live one instead of switching sources under a running stream. */
  return (
    <Show
      when={props.subagent.opened}
      fallback={<TranscriptBody subagent={props.subagent} />}
    >
      <TranscriptBody subagent={props.subagent} />
    </Show>
  );
}

function TranscriptBody(props: { subagent: Subagent }): JSX.Element {
  const transcript = useSubagentTranscript(() => props.subagent);

  return (
    <div class="flex flex-col gap-2">
      {/* Messages before `loading`, and both after the error: a live sub-agent re-reads
          this as it works, and a re-read that blanked the pane back to "Reading the
          transcript…" every few seconds would make the one card being watched the one
          card nobody can read. */}
      <Switch>
        <Match when={transcript.error()}>
          <Text variant="caption" tone="alert">
            That sub-agent's transcript could not be read.
          </Text>
        </Match>
        <Match when={transcript.messages()}>
          {(messages) => {
            /* Iterated by id: each poll returns fresh message objects, and a
               reference-keyed list would remount every turn and lose what its work
               log has already seen arrive. */
            const ids = createMemo(() => messages().map((m) => m.id));
            const byId = createMemo(
              () => new Map(messages().map((m) => [m.id, m] as const)),
            );
            return (
              <For each={ids()}>
                {(id) => (
                  <Show when={byId().get(id)}>
                    {(message) => (
                      <Show
                        when={message().role === "assistant"}
                        fallback={
                          <Text
                            variant="micro"
                            tone="dim"
                            class="whitespace-pre-wrap"
                          >
                            {message().content}
                          </Text>
                        }
                      >
                        <TurnBlocks
                          blocks={message().blocks}
                          streaming={transcript.streaming(message())}
                        />
                      </Show>
                    )}
                  </Show>
                )}
              </For>
            );
          }}
        </Match>
        <Match when={transcript.loading()}>
          <LoadingText label="Reading the transcript" />
        </Match>
      </Switch>
      {/* Live, the turn itself shows the progress. */}
      <Show when={!transcript.live && isLive(props.subagent)}>
        <Text variant="caption" tone="dim">
          Still working — this updates as it goes.
        </Text>
      </Show>
    </div>
  );
}
