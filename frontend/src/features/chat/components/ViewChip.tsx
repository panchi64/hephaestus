import { Show, type JSX } from "solid-js";
import { Icon, StatusFlag, Text, cx, type IconName } from "~/ui";
import { parseInstant } from "~/lib/format";
import { detectContentKind, type ViewItem } from "../viewport/viewItems";

/** Fallback kind word derived from `icon` alone, for callers that only have a
 *  chip icon (no full `ViewItem`) — e.g. the transcript's inline chips, which
 *  reference a version/document/live block, not the consolidated View list. */
const ICON_WORD: Partial<Record<IconName, string>> = {
  play: "Live",
  image: "Image",
  eye: "Snapshot",
  file: "File",
};

/** A coarse glyph + short kind word for a View item, so a chip/cell states what
 *  it opens before the click. Live overlays a snapshot's preview but still reads
 *  "Live" (the running server); a document reads "Doc"; a snapshot whose stamped
 *  preview is an image reads "Image"; anything else reads "Snapshot". */
export function classifyViewItem(item: ViewItem): {
  icon: IconName;
  word: string;
} {
  if (item.live) return { icon: "play", word: "Live" };
  const kind = detectContentKind(null, item.snapshot?.preview?.kind ?? null);
  if (kind === "image") return { icon: "image", word: "Image" };
  return { icon: "eye", word: "Snapshot" };
}

/** The `V{n}` version label baked into `item.label` by `collectViewItems`
 *  (every entry gets one, regardless of kind) — reused here rather than
 *  re-deriving the item's position. */
export function viewItemVersionLabel(item: ViewItem): string | undefined {
  const m = /^V(\d+)/.exec(item.label);
  return m ? `V${m[1]}` : undefined;
}

/** `HH:MM` (operator-local) the item was minted, when its snapshot carries a
 *  `createdAt` — absent for a standalone live head. */
export function viewItemTimeLabel(item: ViewItem): string | undefined {
  const iso = item.snapshot?.createdAt;
  if (!iso) return undefined;
  const d = new Date(parseInstant(iso));
  if (Number.isNaN(d.getTime())) return undefined;
  const p = (n: number) => n.toString().padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * A compact, clickable marker in the transcript for something the agent put in the
 * conversation's View — a snapshot version, a document version, or the live head.
 * Clicking opens it in the viewport, so the transcript stays a readable narrative
 * while the heavy render lives beside it (and older versions stay reachable from
 * where they happened). States what it opens before the click: a kind glyph + a
 * short kind word, the title, and a trailing version/time.
 */
export function ViewChip(props: {
  icon: IconName;
  label: string;
  live?: boolean;
  onOpen: () => void;
  /** Short kind word before the title (SNAPSHOT / DOC / LIVE / IMAGE / …). Falls
   *  back to a word derived from `icon` when the caller has no richer `ViewItem`
   *  to classify. */
  kindWord?: string;
  /** Trailing meta — a version label ("V3") or an HH:MM time. */
  meta?: string;
  /** Dims + tags the chip NEW — an unseen item (index >= the operator's seen
   *  count). Monochrome brightness only, never a color. */
  isNew?: boolean;
}): JSX.Element {
  const kindWord = () => props.kindWord ?? ICON_WORD[props.icon] ?? "View";
  return (
    <button
      type="button"
      onClick={() => props.onOpen()}
      class={cx(
        // A bare row in the tool rows' own anatomy — glyph, mono kind, detail,
        // chevron — with no fill, radius or shadow at rest (design §6: nothing in
        // the flow floats). The hover fill is what says it opens something; a
        // lifted chip was a second container vocabulary in a turn of unboxed rows.
        "group/chip flex w-full items-center gap-2",
        // `py-1` + 12px glyphs: a 24px row, the height of the process rows it
        // sits among. At `py-2` with 14px glyphs it was the tallest row in a
        // turn that wasn't prose, for a handle whose job is to be clicked.
        "px-2 py-1 text-left transition-colors hover:bg-raised",
        props.isNew && "opacity-80",
      )}
    >
      <Icon
        name={props.icon}
        size={12}
        class="shrink-0 text-dim transition-colors group-hover/chip:text-text"
      />
      <Text variant="meta" tone="default">
        {kindWord()}
      </Text>
      <Text variant="micro" tone="dim" class="min-w-0 flex-1 truncate">
        {props.label}
      </Text>
      <Show when={props.meta}>
        <Text variant="micro" tone="dim">
          {props.meta}
        </Text>
      </Show>
      <Show when={props.isNew}>
        <Text variant="micro" tone="dim" class="tracking-label">
          New
        </Text>
      </Show>
      <Show when={props.live}>
        <StatusFlag status="live" dot pulse>
          Live
        </StatusFlag>
      </Show>
      <Icon
        name="chevron-right"
        size={12}
        class="shrink-0 text-dim transition-colors group-hover/chip:text-bright"
      />
    </button>
  );
}
