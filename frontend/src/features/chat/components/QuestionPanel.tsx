import { createUniqueId, type JSX } from "solid-js";
import { ChoiceList, Text, type ActionButtonSpec } from "~/ui";
import type { QuestionReply, QuestionSpec } from "../model";

/**
 * **The agent asking, and the operator answering** — one question, on its own page.
 *
 * It used to render a whole parked call, looping over its one-to-four questions, while
 * the dock looped again over the calls. Two loops meant a park could stand six blocks of
 * radio buttons over a transcript the dock had already taken the composer from. The
 * sequence is `parkPages.ts`'s now, and what is left here is one question — which is also
 * the only thing this file was ever really about.
 *
 * The written answer is always there, under every question, in both modes. That is the
 * point of the whole surface and not a fallback: the options are the model's guesses at
 * what the operator might say, never the range of what they are allowed to say. A form
 * that only offered the guesses would quietly convert "here is what I think you want"
 * into "choose one of these", which is a different and much worse question.
 *
 * **The shape is `ChoiceList`'s** (design §10.18): the question flush under the dock's
 * band, the answers as edge-to-edge ruled rows with numbered keycaps, the free-text row,
 * and one footer carrying the submit — which the dock hands in, together with its own
 * Stop and Back, so the card has one footer rather than two. In a single-choice question
 * typing an answer of one's own deselects the options: the words *are* the answer then.
 * A multi-select keeps its ticks while the operator writes, because there the words
 * qualify the picks.
 *
 * It holds no state. The reply lives with the rest of the park's draft on the controller,
 * because the dock is mounted by a `Show` and anything kept inside it is lost to a
 * re-render — which is exactly what a steering message arriving mid-park used to cause.
 */
export function QuestionPanel(props: {
  question: QuestionSpec;
  /** What has been said to it so far, from the park's draft. */
  reply: QuestionReply | undefined;
  onChange: (reply: QuestionReply) => void;
  /** The footer's primary — advance a page, or settle the park on the last one. */
  onSubmit: () => void;
  submitLabel?: string;
  submitDisabled?: boolean;
  /** The dock's own cells (Stop, Back), sharing the one footer. */
  secondary?: ActionButtonSpec[];
  disabled?: boolean;
}): JSX.Element {
  const questionId = createUniqueId();
  const selections = (): string[] => props.reply?.selections ?? [];
  const written = (): string => props.reply?.text ?? "";

  const pick = (label: string | null) => {
    if (label === null) {
      props.onChange({ selections: [], text: written() });
      return;
    }
    const at = selections();
    const next = !props.question.multiSelect
      ? [label]
      : at.includes(label)
        ? at.filter((l) => l !== label)
        : [...at, label];
    props.onChange({ selections: next, text: props.reply?.text });
  };

  return (
    <div class="flex flex-col">
      <Text
        as="p"
        id={questionId}
        variant="body"
        tone="bright"
        class="px-3 pt-3 pb-2 break-words"
      >
        {props.question.question}
      </Text>
      <ChoiceList
        labelledBy={questionId}
        options={props.question.options.map((o) => ({
          id: o.label,
          label: o.label,
          hint: o.description || undefined,
        }))}
        multiple={props.question.multiSelect}
        selected={selections()}
        value={selections()[0] ?? null}
        onChange={pick}
        freeText={{
          value: written(),
          onInput: (text) => props.onChange({ selections: selections(), text }),
        }}
        onSubmit={props.onSubmit}
        submitLabel={props.submitLabel}
        submitDisabled={props.submitDisabled}
        secondary={props.secondary}
        disabled={props.disabled}
      />
    </div>
  );
}
