/**
 * The answer reveal's DOM half — wrapping characters so they resolve in, and taking the
 * wrappers back out once they have.
 *
 * The scheduling math lives in `streamReveal.ts` and is pure; this is the part that
 * touches nodes. Three rules shape all of it.
 *
 * **Only the blocks inside the reveal window are touched.** Walking the whole answer per
 * delta was O(answer) work per token for a bounded result, and it re-entered blocks that
 * had long since settled and nested a second span inside each existing one. A block
 * enters `schedules` when it starts being wrapped and leaves as soon as its last fade is
 * over, so the per-delta cost is set by the window rather than by the length of the
 * answer — but a block still in that window IS re-wrapped, because `Markdown` rebuilds it
 * out from under its wrappers on the delta that ends it. See `applyReveal`.
 *
 * **The schedule is LOCAL to that block, and this is load-bearing.** An earlier version
 * indexed one schedule across the whole answer (`base + local`, with each earlier block's
 * character count cached). That is wrong, because the animatable character space is not
 * stable: `animatableText` rejects machine-voice subtrees, so a fenced code block that
 * lexes as a paragraph while its closing fence is still missing counts N characters and
 * then counts ZERO the moment it becomes a `<pre>`. Every later character's index shifts
 * down by N, lands on schedule entries whose fades finished long ago, and `revealDelay`
 * returns `null` — which renders them as bare text with no wrapper and no animation. The
 * measured symptom was 73 characters sitting at opacity 0 vanishing in a single frame,
 * the answer snapping into place instead of resolving. Indexing within the block removes
 * the failure rather than compensating for it: nothing an earlier block does can move a
 * later block's indices, because they no longer share a space.
 *
 * **Every wrapper is taken back out.** Nothing did this originally, so every character
 * that passed through the reveal window kept its span, its inline `animation-delay` and
 * its filled animation for the life of the session — ~280 per answer, permanently. A
 * finished fade is invisible; the span it left behind is not, and a one-character span is
 * a one-character text-shaping run, paid again on every repaint of a scrolling
 * transcript. A block is unwrapped only once its last character's fade is over, so
 * unwrapping can never cut one short.
 */

import {
  REVEAL_MS,
  extendSchedule,
  firstLiveIndex,
  revealRuns,
} from "../streamReveal";

/** Subtrees the fade must not enter, because their contents are the machine's
 *  voice and the machine does not ease (§8). Code and samples are mono by
 *  element; `.katex` is neither voice, and threading spans through KaTeX's
 *  markup would be meddling with a layout we don't own. `.font-mono` catches
 *  anything that opted in by class.
 *
 *  `table` is here for three reasons that agree. A table is a dense data panel
 *  (§10) whose header band is already mono because a column header names a
 *  machine field — and it is mono by `font-family`, not by a class, so nothing
 *  else in this list would have caught it. Its cells are emitted values, which
 *  is the §2 test for the machine voice. And the eye reads a grid in two
 *  dimensions, so a reveal sweeping left-to-right through cells reads as
 *  flicker rather than as arrival — on top of the column widths still
 *  reflowing as rows stream in.
 *
 *  `.heph-open-path` is a file the answer pointed at (markdownLinks.ts). It is
 *  mono by rule rather than by the `.font-mono` class, so nothing above catches
 *  it, and it is machine voice by the same §2 test the table is: a path is a
 *  literal string the machine will act on, not a phrase being spoken. */
const MACHINE_VOICE =
  "code, pre, kbd, samp, table, .katex, .font-mono, .heph-open-path";

const SPAN_CLASS = "heph-token-in";

/** The reveal's carry-over between deltas. One per `AnswerText`. */
export interface RevealState {
  /** One schedule per block, by `data-block-index`, each in that block's OWN character
   *  space. A block is entered here when it starts being wrapped and removed once its
   *  last fade is over, so the map holds only the blocks inside the reveal window —
   *  usually one, never more than the window can span. */
  schedules: Map<number, number[]>;
  /** Whether a pass has run. The first one treats whatever is already on screen as
   *  settled, so attaching to a turn already under way doesn't animate the whole answer
   *  back in from the beginning. */
  seeded: boolean;
}

export function createRevealState(): RevealState {
  return { schedules: new Map(), seeded: false };
}

/** Every animatable text node under `root`, in document order, with the character index
 *  each one starts at *within `root`*. Machine-voice subtrees are rejected outright, so
 *  their characters are neither wrapped nor counted — which is what makes a code block
 *  land hard inside an answer easing in around it. Collected before any mutation, since
 *  splitting a node mid-walk would invalidate the walker. */
function animatableText(root: HTMLElement): {
  nodes: { node: Text; base: number }[];
  count: number;
} {
  const walker = document.createTreeWalker(
    root,
    NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
    {
      acceptNode: (node) => {
        if (node.nodeType !== Node.ELEMENT_NODE)
          return NodeFilter.FILTER_ACCEPT;
        // REJECT prunes the whole subtree; SKIP passes over the element itself
        // and keeps descending, which is what every other element wants.
        return (node as Element).matches(MACHINE_VOICE)
          ? NodeFilter.FILTER_REJECT
          : NodeFilter.FILTER_SKIP;
      },
    },
  );
  const nodes: { node: Text; base: number }[] = [];
  let count = 0;
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const node = n as Text;
    nodes.push({ node, base: count });
    count += node.data.length;
  }
  return { nodes, count };
}

/** A run of characters that resolve together. The delay carries the run's phase
 *  across a re-render; the duration is `--token-reveal-ms` on the answer's host. */
function revealSpan(text: string, delay: number): HTMLSpanElement {
  const span = document.createElement("span");
  span.className = SPAN_CLASS;
  span.style.animationDelay = `${delay}ms`;
  span.textContent = text;
  return span;
}

/**
 * The units the answer is walked in: `Markdown streamStable`'s top-level blocks, or the
 * host itself when there are none (the default render path, or an answer too short to
 * have produced a block wrapper yet).
 *
 * A plain descendant query rather than a child combinator because `Markdown` puts its
 * blocks inside its own `.heph-prose` element, and an answer never nests a second
 * `Markdown` inside itself — the only way another `[data-block-index]` could appear here.
 */
function revealUnits(host: HTMLElement): HTMLElement[] {
  const blocks = host.querySelectorAll<HTMLElement>("[data-block-index]");
  return blocks.length ? Array.from(blocks) : [host];
}

/** A block's own index, which survives the re-render its element does not. Falls back to
 *  document order for the `[host]` case, which has no attribute and only one unit. */
function unitIndex(unit: HTMLElement, position: number): number {
  const raw = unit.dataset.blockIndex;
  return raw === undefined ? position : Number(raw);
}

/** Put every wrapper in `unit` back as plain text and re-join the runs it split.
 *  `normalize()` is the half that matters for paint: without it the block keeps one text
 *  node per run and is shaped a run at a time. */
export function unwrapReveal(unit: HTMLElement): void {
  const spans = unit.querySelectorAll<HTMLElement>(`.${SPAN_CLASS}`);
  if (!spans.length) return;
  spans.forEach((span) =>
    span.replaceWith(document.createTextNode(span.textContent ?? "")),
  );
  unit.normalize();
}

/** Unwrap every block — the terminal flush, run once the last fade is over and no more
 *  text is coming. The tail block never stops being the tail on its own, so without this
 *  every answer would keep its final block's wrappers for good. */
export function unwrapAll(host: HTMLElement): void {
  revealUnits(host).forEach(unwrapReveal);
}

/** When the last character scheduled anywhere finishes resolving, or 0 when nothing is
 *  pending — what the terminal flush is armed against. */
export function revealEndsAt(state: RevealState): number {
  let end = 0;
  for (const starts of state.schedules.values())
    if (starts.length)
      end = Math.max(end, starts[starts.length - 1] + REVEAL_MS);
  return end;
}

/** When every character in `starts` has finished resolving. */
function settled(starts: number[], now: number): boolean {
  return !starts.length || now >= starts[starts.length - 1] + REVEAL_MS;
}

/**
 * Wrap every block that still has a fade in flight, and give back the wrappers of any
 * block whose fade is over. Mutates `state`.
 *
 * **A block behind the tail is re-wrapped, not just left alone, and that is the whole
 * reason this loops.** `Markdown` re-renders a block whenever its source string changes,
 * which includes the delta that ends it — the paragraph gains the blank line that closes
 * it as the next block opens. That rebuild destroys the wrappers of every character in it
 * that is still fading, and a rule of "only the tail is wrapped" never puts them back:
 * measured, ~90 characters at partial opacity snapping to full at each block boundary.
 * The loop is bounded by the reveal window rather than by the length of the answer,
 * because a block leaves `schedules` as soon as its last character has settled.
 *
 * Rebuilding a block's wrappers wholesale is deliberate — its DOM is new anyway, and
 * re-deriving each character's delay from its *absolute start time* is exactly what lets
 * a fade continue across the rebuild rather than restarting.
 */
export function applyReveal(
  host: HTMLElement,
  state: RevealState,
  now: number,
  interval: number,
): void {
  const units = revealUnits(host);
  const last = units.length - 1;
  const byIndex = new Map<number, HTMLElement>();
  units.forEach((unit, i) => byIndex.set(unitIndex(unit, i), unit));
  const tail = unitIndex(units[last], last);

  for (const [index, starts] of [...state.schedules]) {
    if (index === tail) continue;
    const unit = byIndex.get(index);
    if (settled(starts, now)) {
      // Its last fade is over, so the wrappers have done their job and the block goes
      // back to being plain, re-joined text.
      if (unit) unwrapReveal(unit);
      state.schedules.delete(index);
    } else if (unit) {
      rewrap(unit, starts, now);
    }
  }

  // Unwrap FIRST, then measure and wrap — the same order `rewrap` keeps, for the same
  // reason: walking a block that still holds wrappers finds the text inside each and
  // wraps it again, a span inside a span, multiplying the wrappers on every delta.
  //
  // On the tail specifically this is DEFENSIVE rather than load-bearing, and removing it
  // does not fail the tests: `Markdown` rebuilds the trailing block on nearly every
  // delta, so it is already plain text by the time this runs. It stays because "nearly"
  // is doing real work in that sentence — a delta that leaves the tail's source untouched
  // is possible, and the cost here is one no-op query.
  unwrapReveal(units[last]);
  const { nodes, count } = animatableText(units[last]);
  let starts = state.schedules.get(tail) ?? [];
  if (!state.seeded) {
    state.seeded = true;
    // Attaching to a passage that already has text — a resumed stream, or a turn already
    // under way when this mounted: treat what is on screen as settled rather than
    // animating the whole answer in from the start. A fresh passage has no characters
    // yet, so this seeds nothing.
    starts = Array.from({ length: count }, () => now - REVEAL_MS);
  }
  starts = extendSchedule(starts, count, now, interval);
  state.schedules.set(tail, starts);
  wrapNodes(nodes, starts, now);
}

/** Take a block back to plain text and wrap it again from its schedule. The delays are
 *  recomputed from absolute start times, so a fade that was in flight resumes at its own
 *  phase instead of restarting — which is what makes rebuilding safe. */
function rewrap(unit: HTMLElement, starts: number[], now: number): void {
  unwrapReveal(unit);
  wrapNodes(animatableText(unit).nodes, starts, now);
}

/** Split each node at the reveal front and rebuild only what is still resolving. */
function wrapNodes(
  nodes: { node: Text; base: number }[],
  starts: number[],
  now: number,
): void {
  const from = firstLiveIndex(starts, now);
  if (from >= starts.length) return;
  for (const { node, base } of nodes) {
    const len = node.data.length;
    if (base + len <= from || !node.parentNode) continue;
    // Split the node once into "settled" and "still resolving", then rebuild
    // only the second half, one node per run.
    const cut = Math.max(0, from - base);
    const frag = document.createDocumentFragment();
    if (cut > 0)
      frag.appendChild(document.createTextNode(node.data.slice(0, cut)));
    let at = base + cut;
    for (const { end, delay } of revealRuns(starts, at, base + len, now)) {
      const text = node.data.slice(at - base, end - base);
      frag.appendChild(
        delay === null
          ? document.createTextNode(text)
          : revealSpan(text, delay),
      );
      at = end;
    }
    node.parentNode.replaceChild(frag, node);
  }
}
