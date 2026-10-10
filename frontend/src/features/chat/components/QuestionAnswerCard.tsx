import { For, Show, type JSX } from "solid-js";
import { ActionFrame, Stack, Text } from "~/ui";
import type { AnsweredQuestion } from "../model";

/** The row's index in the machine's own voice — `01`, `02`. Padded so a card with ten
 *  pairs keeps one gutter width and the questions stay left-aligned with each other. */
const ordinal = (index: number): string => String(index + 1).padStart(2, "0");

/**
 * **What the agent asked, and what it was told** — the settled half of the dock, kept in
 * the transcript.
 *
 * It exists because the answer used to leave no readable trace. The question is asked in
 * a dock that takes over the composer and disappears the moment it is submitted, and the
 * only record afterwards was the `ask_user` call's own result: prose, rendered as a
 * generic tool row, inside a work log that folds shut. So a thread would read as the
 * agent asking nothing and then acting on something — the operator's own contribution to
 * the turn, missing from the account of it.
 *
 * **It is the agent's row, not the operator's.** Rendering the answers as a user bubble
 * was the obvious alternative and is worse: it would credit the operator with a message
 * they never typed, sitting in a transcript where every other user bubble is one they
 * did, and a reload would have to synthesise the same fiction to agree with itself.
 * What actually happened is that a call the agent made came back carrying their answer,
 * so it renders where that call is.
 *
 * **The question's own words come from the backend**, paired with the reply from the
 * parked call's arguments (warm, on `question.answered`; cold, from the same pairing in
 * the conversation projection). This component restates rather than re-derives, so a
 * reload cannot word a question differently from the way it was asked.
 *
 * ── Why it is a ruled band and not a card ──
 * It is a **record of N entries with one shape**, which is the case design §7 keeps a
 * border for and §10.3 gives square corners: the hairlines rule one question off the
 * next and align every answer down a single gutter, which is structural work neither
 * space nor surface value can do here. Shadow is deliberately absent — a grid is not a
 * layer (§6.4). The first version was a rounded, sunken card whose four lines were all
 * one size, so it read as a paragraph of alternating greys with no way in.
 *
 * ── The ladder, which is the whole point ──
 * Four steps, in the order §4 asks for them (size → weight → brightness):
 * the band's header and the row indices are `meta`/`micro` **mono** because they are
 * machine labels; the question is a sans `label`, dim, because that is literally what it
 * is (§10.1 — a label over its value); the answer is the **value**, and it takes the
 * transcript's reading scale rather than the chrome scale, because these are the
 * operator's own words sitting inches from the model's prose — the same argument that
 * puts `UserTurn` on `reading`; anything they wrote *beside* a choice drops back to
 * `body`, subordinate to the choice it qualifies.
 */
export function QuestionAnswerCard(props: {
  answers: AnsweredQuestion[];
}): JSX.Element {
  return (
    // The action tier's frame (design §10.18), settled: the same blue band the ask
    // wore in the dock, without the live square — so the record reads as the answer
    // to *that* ask rather than as a second, unrelated kind of panel. The frame pads
    // nothing; the rows bring their own gutter, flush under the band as the live
    // question's were.
    <ActionFrame
      tone="question"
      live={false}
      title="Asked you"
      aside={
        // The one diegetic figure this panel spends (§11), at the band's edge.
        <span class="tabular-nums">{props.answers.length} answered</span>
      }
    >
      <div class="divide-y divide-line">
        <For each={props.answers}>
          {(answer, index) => (
            <div class="flex gap-3 px-3 py-2">
              <Text
                variant="micro"
                tone="dim"
                class="w-4 shrink-0 pt-1 tabular-nums"
              >
                {ordinal(index())}
              </Text>
              <Stack gap={1} class="min-w-0 flex-1">
                <Text variant="label" tone="dim" class="break-words">
                  {answer.question}
                </Text>
                {/* The choice reads `bright` at the reading scale, because it is the
                    operator's word in a row of the agent's. Where they chose nothing the
                    written answer takes that position rather than sitting under an empty
                    one — the options were the model's guesses, never the range of
                    allowed replies, and a card implying otherwise would misreport what
                    the surface offered. */}
                <Show
                  when={answer.selections.length > 0}
                  fallback={
                    <Show when={answer.text}>
                      <Text
                        variant="reading"
                        tone="bright"
                        class="whitespace-pre-wrap break-words"
                      >
                        {answer.text}
                      </Text>
                    </Show>
                  }
                >
                  <Text variant="reading" tone="bright" class="break-words">
                    {answer.selections.join(" · ")}
                  </Text>
                  <Show when={answer.text}>
                    <Text
                      variant="body"
                      tone="default"
                      class="whitespace-pre-wrap break-words"
                    >
                      {answer.text}
                    </Text>
                  </Show>
                </Show>
              </Stack>
            </div>
          )}
        </For>
      </div>
    </ActionFrame>
  );
}
