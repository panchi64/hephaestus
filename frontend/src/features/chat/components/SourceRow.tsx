/**
 * One row of the source inventory, and the two readouts inside it.
 *
 * Split out of `SourcesSurface` when the panel grew a second section: the surface owns
 * what the inventory *is* — its shelves, its figures, its empty state — and a row owns
 * how one source presents itself. They change for different reasons, which is the split.
 */

import { Show, type JSX } from "solid-js";
import { Chip, Text } from "~/ui";
import { hostLabel, relativeTime } from "~/lib/format";
import type { SourceItem } from "../viewport/sourceItems";
import { Sep, createAdoptedOpen } from "./ProcessRow";
import { SurfaceCard } from "./surfaceChrome";

/** One source: what it is, where it came from, and — opened — the passage the run
 *  actually saw. */
export function SourceRow(props: { item: SourceItem }): JSX.Element {
  const s = () => props.item;
  const { open, toggle } = createAdoptedOpen({});
  const hasBody = () =>
    Boolean(s().snippet) || Boolean(s().url) || Boolean(s().ref);
  /** The row's name. A page's title, else its host; a passage's locator, else its
   *  source. Never the bare url — a row of full urls is a column nobody can scan. */
  const name = () =>
    s().title ||
    (s().url ? hostLabel(s().url!) : (s().ref ?? s().origin)) ||
    "Untitled";

  return (
    <SurfaceCard
      open={open()}
      onToggle={toggle}
      icon={s().kind === "corpus" ? "library" : "link"}
      label={s().kind === "corpus" ? "Passage" : "Page"}
      title={name()}
      detail={name()}
      /* Guards the content rather than the disclosure, so a row with nothing behind
         it still toggles without revealing an empty band. */
      hasBody={hasBody()}
      trailing={
        <>
          {/* **How many times this thread came back to it.** A source seen once and a
              source seen four times are different things, and the count is the only
              evidence of that the fold leaves behind — everything else about the
              repeat sightings has been merged away. */}
          <Show when={s().sightings > 1}>
            <Text variant="micro" tone="dim" class="tabular-nums">
              ×{s().sightings}
            </Text>
          </Show>
        </>
      }
    >
      <SourceDates item={s()} />
      <Show when={s().snippet}>
        {/* Reading scale, not `micro`. This is the one thing on the panel written
            by a human for humans — everything around it is the machine's record
            of what it did with it. */}
        <Text as="p" variant="body" tone="default" class="break-words">
          {s().snippet}
        </Text>
      </Show>
      <SourceAddress item={s()} />
    </SurfaceCard>
  );
}

/** The two dates, kept apart because they answer different questions and only one of
 *  them can be trusted to be a date at all.
 *
 *  `published` is the source's own, passed through by the backend exactly as its
 *  provider reported it and never parsed — so it is printed verbatim, whatever shape it
 *  arrived in. `retrievedAt` is the backend's own ISO stamp for when *this run* read it,
 *  which is safe to make relative. Rendering both through one formatter would mean
 *  parsing a string the backend deliberately did not. */
function SourceDates(props: { item: SourceItem }): JSX.Element {
  const s = () => props.item;
  return (
    <Show when={s().published || s().retrievedAt}>
      <div class="flex flex-wrap items-baseline gap-x-2">
        <Show when={s().published}>
          <Text variant="micro" tone="dim">
            published {s().published}
          </Text>
        </Show>
        <Show when={s().published && s().retrievedAt}>
          <Sep />
        </Show>
        <Show when={s().retrievedAt}>
          <Text variant="micro" tone="dim">
            read {relativeTime(s().retrievedAt!)}
          </Text>
        </Show>
      </div>
    </Show>
  );
}

/** Where it lives. A page gets a chip that opens it; a corpus passage gets its locator
 *  as a static token, because there is no tab to open it in and a chip that opened
 *  `about:blank` would read as a broken link rather than as a source on the operator's
 *  own disk. The same rule the transcript's Sources row follows. */
function SourceAddress(props: { item: SourceItem }): JSX.Element {
  const s = () => props.item;
  return (
    <div class="flex flex-wrap items-center gap-2">
      <Show
        when={s().url}
        fallback={
          <Show when={s().ref}>
            <Chip leading="library">{s().ref}</Chip>
          </Show>
        }
      >
        <Chip
          leading="link"
          onClick={() => window.open(s().url!, "_blank", "noopener,noreferrer")}
        >
          {hostLabel(s().url!)}
        </Chip>
      </Show>
      <Show
        when={s().kind === "corpus" && s().origin && s().origin !== s().ref}
      >
        <Text variant="micro" tone="dim" class="min-w-0 truncate">
          {s().origin}
        </Text>
      </Show>
    </div>
  );
}
