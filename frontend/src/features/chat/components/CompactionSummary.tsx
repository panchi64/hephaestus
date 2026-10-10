import {
  For,
  Show,
  createSignal,
  onCleanup,
  onMount,
  type JSX,
} from "solid-js";
import { ScrollFade, Stack, Text, cx } from "~/ui";
import type { SummarySection } from "~/lib/stream";
import { CompactionRow as Row } from "./CompactionRow";

/** What the model is left holding after a fold — one ruled row per section.
 *
 *  **Read as a readout, not as a passage.** A summary stands in for a whole stretch of a
 *  conversation, so rendered flat it is a screenful of undifferentiated prose sitting in
 *  the middle of the transcript, which is the one shape guaranteed not to be read. The
 *  summarizer is asked for eight fixed headings and the backend parses them, so the
 *  structure is already there; this sets it so the operator can *scan*: every heading
 *  engraved at once, each body clamped to a few lines, and one control on the section they
 *  actually want.
 *
 *  That is deliberately not the closed chevron this surface replaced. A chevron hid
 *  whether there was anything at all; this shows what the model remembers *about* — the
 *  decisions, the anchors, the open questions — and only the depth of each is behind a
 *  control.
 *
 *  **Mono throughout, like the panel around it** (§2): a checkpoint is machine output from
 *  end to end, and the one thing it must not read as is another contribution to the
 *  conversation.
 *
 *  Three rules decide how a section is set, and all three come off the wire rather than
 *  being guessed here:
 *
 *  - **The machine voice is for Anchors.** Its whole purpose is exact paths, ids and
 *    numbers carried across a fold character for character, so it takes the tightest mono
 *    step rather than the reading one. Every other section is prose.
 *  - **Tool-derived text is attributed, never quoted silently.** A checkpoint is replayed
 *    to the model as a user-shaped message — the most authoritative voice in the history —
 *    and one section repeats what a web page or a file said. The backend fences it for the
 *    model; the operator gets the same fact as a bordered, labelled block. The fence's
 *    *markers* stay behind: the nonce is addressed to the model and is noise here.
 *  - **A section with no heading is a checkpoint that didn't parse.** Old checkpoints and
 *    summarizer drift degrade to one keyless section holding the whole text. That is the
 *    one case where a wall of prose is the honest rendering — so it is clamped hardest and
 *    legended `Briefing`, which claims no structure for it. Not `Summary`: the panel
 *    already has a row by that name carrying the figures and the explanation, and a second
 *    one wearing the same legend reads as the first repeating itself.
 */
export function CompactionSummary(props: {
  sections: SummarySection[];
}): JSX.Element {
  return (
    <For each={props.sections}>
      {(section) => (
        <Row legend={section.key || "Briefing"}>
          <Show
            when={section.untrusted}
            fallback={<SectionBody section={section} />}
          >
            {/* Bordered and labelled: the same "this came from outside" the fence makes
                to the model, in the form the operator reads. The border and the words
                both carry it — colour never carries a distinction alone. `Stack` rather
                than a bare div because `Text` is inline: the attribution has to sit
                *above* the quote, not run into its first line. */}
            <Stack gap={1} class="border-line border-l pl-2">
              <Text variant="micro" tone="dim">
                QUOTED FROM TOOL OUTPUT — NOT THIS WORKSPACE'S OWN WORDS
              </Text>
              <SectionBody section={section} />
            </Stack>
          </Show>
        </Row>
      )}
    </For>
  );
}

/** A section's text, clamped to a few lines with the rest one control away.
 *
 *  `micro` is the tightest mono step and is where Anchors belongs — a column of exact
 *  paths and ids is read by looking *down* it, and the smaller step keeps more of it on
 *  screen at once. Everything else takes `code`, the mono step §4 keeps for content set
 *  inside chrome. Both wrap on the stored line breaks, which are load-bearing in a
 *  bulleted list.
 *
 *  **Clamped by height, and the control is measured into existence.** Most sections are a
 *  sentence or two and need none at all; a MORE that expands nothing is worse than no
 *  MORE, so the toggle appears only where the text actually overflows. An unparsed
 *  checkpoint — one keyless section holding everything — gets the tighter clamp, because
 *  it is the case that would otherwise be the whole screen.
 *
 *  **Re-measured whenever the box resizes**, not only at mount. The clamp stays applied
 *  whatever layout does next, so a section that fit when it mounted — before the mono
 *  face loaded, or at a wider panel — would otherwise be cut off later with no control
 *  to open it. Only while closed: an open section is not clamped, so it cannot tell. */
function SectionBody(props: { section: SummarySection }): JSX.Element {
  const [open, setOpen] = createSignal(false);
  const [overflows, setOverflows] = createSignal(false);
  let body: HTMLDivElement | undefined;

  onMount(() => {
    if (!body) return;
    const el = body;
    const measure = () => {
      if (!open()) setOverflows(el.scrollHeight > el.clientHeight + 4);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    onCleanup(() => observer.disconnect());
  });

  const clamp = () => (props.section.key ? "max-h-20" : "max-h-28");

  return (
    <div>
      <div
        ref={body}
        class={cx("relative overflow-hidden", !open() && clamp())}
      >
        <Text
          variant={props.section.voice === "machine" ? "micro" : "code"}
          tone="dim"
          class="block break-words whitespace-pre-wrap"
        >
          {props.section.body}
        </Text>
        {/* Says there is more below without spending a second control on saying it —
            the toggle underneath is already the answer to how much more. */}
        <Show when={!open() && overflows()}>
          <ScrollFade variant="clamp" edge="bottom" ground="surface" />
        </Show>
      </div>
      <Show when={overflows()}>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          class="group mt-1"
        >
          <Text variant="meta" tone="dim" class="group-hover:text-bright">
            {open() ? "Show less" : "Show more"}
          </Text>
        </button>
      </Show>
    </div>
  );
}
