import { Show, type JSX } from "solid-js";
import { Collapse, Text } from "~/ui";
import { INJECTION_ICON } from "../contextLabels";
import {
  envelopeLabel,
  noticeLabel,
  type EnvelopeLabel,
  type EnvelopeScope,
} from "../envelopeLabels";
import type { ContentPart } from "../model";
import { ProcessRow, Sep, createAdoptedOpen } from "./ProcessRow";
import { QuotedBlock } from "./QuotedBlock";

/** What the row shows: a wrapped part of a payload, or a notice the chassis wrote. */
type EnvelopeSource =
  | { variant: "part"; part: ContentPart; scope: EnvelopeScope }
  | { variant: "notice"; kind: string | undefined; body: string };

function resolve(source: EnvelopeSource): {
  label: EnvelopeLabel;
  body: string;
  quotedFrom: string | undefined;
} {
  if (source.variant === "notice")
    return {
      label: noticeLabel(source.kind, source.body),
      body: source.body,
      quotedFrom: undefined,
    };
  const { part } = source;
  return {
    label: envelopeLabel(part, source.scope),
    body: part.text,
    quotedFrom: part.kind === "untrusted" ? (part.source ?? "") : undefined,
  };
}

/** A block of text the operator did not write and the model did not produce — a fetched
 *  page, an attachment's text, a project's instruction file, a chassis reminder — set as
 *  its own row instead of inline in the text around it.
 *
 *  Same anatomy and dashed diamond as `ContextInjectionCard`: it is something put in front
 *  of the model, not something anyone said, so it takes no hover fill and rests closed.
 *  The label is a word and not only a glyph, because colour and shape never carry a
 *  distinction alone (§12).
 *
 *  Untrusted text is also quoted behind a hairline with an attribution line, the same
 *  block a compaction checkpoint uses. */
export function EnvelopeRow(
  props: EnvelopeSource & { open?: boolean },
): JSX.Element {
  const { open, toggle } = createAdoptedOpen(props);
  const shown = () => resolve(props);
  const body = (): JSX.Element => (
    <Text
      variant="micro"
      tone="dim"
      class="block max-h-64 overflow-y-auto whitespace-pre-wrap break-words"
    >
      {shown().body}
    </Text>
  );
  return (
    <div>
      <ProcessRow
        open={open()}
        onToggle={toggle}
        icon={INJECTION_ICON}
        frame="diamond"
        dashed
        label={shown().label.label}
        title={shown().label.label}
      >
        <Show when={shown().label.detail}>
          {(detail) => (
            <>
              <Sep />
              <Text variant="micro" tone="dim" class="min-w-0 truncate">
                {detail()}
              </Text>
            </>
          )}
        </Show>
      </ProcessRow>
      <Collapse open={open()}>
        <div class="px-2 py-1.5">
          <Show when={shown().quotedFrom !== undefined} fallback={body()}>
            <QuotedBlock from={shown().quotedFrom || undefined}>
              {body()}
            </QuotedBlock>
          </Show>
        </div>
      </Collapse>
    </div>
  );
}
