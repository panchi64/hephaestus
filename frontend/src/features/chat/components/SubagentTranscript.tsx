import { For, Match, Show, Switch, createMemo, type JSX } from "solid-js";
import { LoadingText, Text } from "~/ui";
import type { Subagent } from "../data";
import { useSubagentTranscript } from "../subagentTranscript";
import { TurnBlocks } from "./TurnBlocks";

/**
 * What a sub-agent did, live: the body of `SubagentDetail`.
 *
 * Rendered with the main room's own `TurnBlocks`, so it reads exactly like the agent's
 * own work. Not `MessageItem`: its edit, fork, rewind and retry act on a thread the
 * operator types into, and nobody types into a sub-agent.
 *
 * The opening prompt is shown even though the operator didn't write it: it is the task
 * the agent was given, the first thing to check when it answered the wrong question.
 */
export function SubagentTranscript(props: { subagent: Subagent }): JSX.Element {
  const transcript = useSubagentTranscript(() => props.subagent);

  return (
    <div class="flex flex-col gap-2">
      {/* Messages before `loading`: a re-read on a status change must not blank the pane. */}
      <Switch>
        <Match when={transcript.error()}>
          <Text variant="caption" tone="alert">
            That sub-agent's transcript could not be read.
          </Text>
        </Match>
        <Match when={transcript.messages()}>
          {(messages) => {
            /* Iterated by id: a re-read returns fresh message objects, and a
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
    </div>
  );
}
