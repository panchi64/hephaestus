/**
 * Every source a thread touched, folded into one inventory.
 *
 * Derived from the transcript's own `citations`, exactly as `commandItems.ts` derives the
 * command log from its terminal blocks — and for the same reason. The fold and the cold
 * read both already produce the citations per turn, so a second fetch would be a second
 * answer to "what did this thread read", and the one that disagreed would be this one.
 * Presentation-only, and therefore thread-scoped without doing anything to be.
 *
 * **A turn's Sources row answers a different question from this panel.** That row is the
 * three-or-so links *this answer* leaned on, numbered within the turn. This is the whole
 * inventory, across every turn, which is the requirement it exists for: "a complete source
 * inventory, not the first three links". So the panel deliberately **does not number**.
 * Two numbering systems over one set of sources is worse than none — `[3]` in the
 * transcript and `[17]` here would be the same page, and an operator who noticed would be
 * right to stop trusting both.
 *
 * **Folding is the backend's rule, applied twice.** `stream/fold.ts` already folds by
 * `key` within a turn, keeping the highest `engagement`; a source read in turn 2 and
 * listed again in turn 7 is one source, so the same rule runs again across turns. It is
 * the same ladder (`ENGAGEMENT_ORDER`, mirrored from `core/citations`) rather than a
 * second opinion about which sighting is the stronger one.
 */

import { ENGAGEMENT_ORDER, type ChatMessage, type Citation } from "../model";
import { hostLabel, parseInstant } from "~/lib/format";

/** How far the run got with a source. Non-optional here: a citation that arrived without
 *  one has already been defaulted, so nothing downstream has to ask twice. */
export type Engagement = NonNullable<Citation["engagement"]>;

/** One source, as the inventory holds it. */
export interface SourceItem {
  /** Identity — the backend's own, because a web source and a corpus passage do not
   *  fold by the same field. Falls back for the cold path, which has not been given
   *  one yet. */
  key: string;
  kind: "web" | "corpus";
  url: string | null;
  title?: string;
  /** A corpus passage's locator — what it has instead of an address. */
  ref?: string;
  engagement: Engagement;
  snippet?: string;
  /** The source's own date, exactly as its provider reported it. Never parsed — see
   *  `recency` for why this is rendered verbatim and the other stamp is not. */
  published?: string;
  /** When *this run* read it. ISO-8601 UTC, so it is safe to parse. */
  retrievedAt?: string;
  /** Where it came from, as one label — a host for the web, the corpus source for a
   *  passage. What "independent" is counted over; see `originCount`. */
  origin: string;
  /** How many separate sightings folded into this one row. A source a thread kept
   *  coming back to is a different thing from one it saw once. */
  sightings: number;
}

/** Which shelf of the inventory a source sits on — how far the run got with it, because
 *  the two questions the operator brings here are "what did this rest on" and "what did
 *  it look at and put down". */
export type SourceBucket = Engagement;

/** The whole inventory, in one shape, so the panel reads it once rather than filtering
 *  the same list four times. */
export interface SourceInventory {
  /** Every source, in order of first sighting. Chronological, never ranked — the same
   *  argument the command log makes: a thread's reading is a sequence. */
  items: SourceItem[];
  /** The buckets, in the order the panel stacks them. A bucket with nothing in it is
   *  absent rather than empty: nothing emits `cited` yet, and a permanently empty
   *  CITED heading is a readout that teaches the operator to ignore headings. */
  groups: { bucket: SourceBucket; items: SourceItem[] }[];
  /** How many distinct origins are behind those sources. */
  originCount: number;
  /** The most recent `retrievedAt` across the inventory, or null where none carried
   *  one. */
  newestRetrieval: string | null;
}

/** The order the buckets read in — strongest standing first. */
const BUCKET_ORDER: readonly SourceBucket[] = ["cited", "read", "listed"];

/** The heading each bucket carries, and what it means in the operator's terms.
 *
 *  `listed` is the one that needs its words chosen carefully. The requirement asks for
 *  "discarded sources, with reasons", and the only reason the system actually holds is
 *  the structural one: a search returned it and nothing ever opened it. Writing a richer
 *  reason would mean inventing one, so the heading says exactly what is known. */
export const BUCKET_LABEL: Record<SourceBucket, string> = {
  cited: "Cited",
  read: "Read",
  listed: "Listed, never opened",
};

export const BUCKET_HINT: Record<SourceBucket, string> = {
  cited: "Carried into the answer.",
  read: "Its text went in front of the model.",
  listed: "A search returned it and nothing opened it.",
};

/** Identity for a citation that arrived without a `key` — the cold path has not been
 *  given one yet, and a fold keyed on `undefined` would collapse the whole inventory
 *  into a single row. The same two rules the backend keys by, in the same order. */
function keyOf(c: Citation): string {
  if (c.key) return c.key;
  if (c.url) return c.url;
  if (c.sourceId || c.ref) return `${c.sourceId ?? ""}:${c.ref ?? ""}`;
  return c.title ?? "";
}

/** Where a source came from, as one word the operator can count.
 *
 *  A host for the web (minus `www.`), the corpus source for a passage. This is what
 *  makes an "independent source count" meaningful rather than a synonym for the row
 *  count: five pages off one site are five sources and one origin, and the difference is
 *  the whole reason the figure is worth printing. */
function originOf(c: Citation): string {
  if (c.url) return hostLabel(c.url);
  if (c.sourceId) return c.sourceId;
  return c.ref ?? c.title ?? "";
}

/** The stronger of two sightings, field by field.
 *
 *  Not "keep the later one" and not "keep the first one" — the two sightings of a source
 *  carry different things. A search hit brings `snippet` and `published`; the fetch that
 *  follows brings `read` and nothing else, because the whole page went into the tool
 *  result beside it. Taking either wholesale throws away what the other knew. */
function merge(into: SourceItem, c: Citation): SourceItem {
  const engagement = c.engagement ?? "listed";
  const stronger =
    ENGAGEMENT_ORDER[engagement] > ENGAGEMENT_ORDER[into.engagement];
  return {
    ...into,
    engagement: stronger ? engagement : into.engagement,
    // The first non-empty value of each wins, whichever sighting brought it, except
    // `retrievedAt`, where the *latest* is the honest answer to "when did this run last
    // look at it".
    title: into.title ?? c.title,
    url: into.url ?? c.url,
    ref: into.ref ?? c.ref,
    snippet: into.snippet ?? c.snippet,
    published: into.published ?? c.published,
    retrievedAt: laterOf(into.retrievedAt, c.retrievedAt),
    sightings: into.sightings + 1,
  };
}

/** The later of two ISO stamps, tolerating an absent or unparseable one on either
 *  side — a stamp the backend could not produce must not blank one it could. */
function laterOf(
  a: string | undefined,
  b: string | undefined,
): string | undefined {
  if (!a) return b;
  if (!b) return a;
  const ta = parseInstant(a);
  const tb = parseInstant(b);
  if (Number.isNaN(ta)) return b;
  if (Number.isNaN(tb)) return a;
  return tb > ta ? b : a;
}

/** Every source the thread touched, folded, in order of first sighting. */
export function collectSources(messages: ChatMessage[]): SourceItem[] {
  const byKey = new Map<string, SourceItem>();
  for (const m of messages)
    for (const c of m.citations ?? []) {
      const key = keyOf(c);
      // A citation with no identity at all cannot be folded and cannot be told apart
      // from the next one; dropping it is better than inventing a row that merges
      // every anonymous source into one.
      if (!key) continue;
      const existing = byKey.get(key);
      byKey.set(
        key,
        existing
          ? merge(existing, c)
          : {
              key,
              kind: c.kind ?? "web",
              url: c.url,
              title: c.title,
              ref: c.ref,
              engagement: c.engagement ?? "listed",
              snippet: c.snippet,
              published: c.published,
              retrievedAt: c.retrievedAt,
              origin: originOf(c),
              sightings: 1,
            },
      );
    }
  return [...byKey.values()];
}

/** The inventory the panel renders — the items, their shelves, and the two figures at
 *  the top. One pass, because the same list is otherwise walked four times to answer
 *  four questions about it. */
export function buildInventory(items: SourceItem[]): SourceInventory {
  const bucketed = new Map<SourceBucket, SourceItem[]>();
  const origins = new Set<string>();
  let newest: string | undefined;
  for (const item of items) {
    const bucket = item.engagement;
    const list = bucketed.get(bucket);
    if (list) list.push(item);
    else bucketed.set(bucket, [item]);
    if (item.origin) origins.add(item.origin);
    newest = laterOf(newest, item.retrievedAt);
  }
  return {
    items,
    groups: BUCKET_ORDER.filter((b) => bucketed.has(b)).map((bucket) => ({
      bucket,
      items: bucketed.get(bucket)!,
    })),
    originCount: origins.size,
    newestRetrieval: newest ?? null,
  };
}
