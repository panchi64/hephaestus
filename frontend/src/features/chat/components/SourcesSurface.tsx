/**
 * Everything a thread read, as one inventory.
 *
 * The transcript already shows the handful of links an answer leaned on, and that is the
 * right place to *follow* one. It is the wrong place to answer the question this panel
 * exists for: **what is all of this resting on, and what did it look at and put down?**
 * In the transcript a source is three chips under one answer, separated from the next
 * turn's three by half a screen of prose — so "did anything else say this" is answered by
 * scrolling and remembering. Here they are one column, filed by how far the run actually
 * got with each.
 *
 * Three things the panel is careful about, each because the honest version is less
 * flattering than the easy one:
 *
 * - **It does not number.** The turn's Sources row numbers within the turn; numbering
 *   again across the thread would make `[3]` there and `[17]` here the same page.
 * - **`Listed, never opened` says exactly what is known.** The only reason the system
 *   holds for an unopened source is the structural one. A richer sentence would be
 *   invented.
 * - **`origins` is printed beside the source count, never instead of it.** Five pages off
 *   one site are five sources and one origin, and an inventory that reported only the
 *   first figure would read as five times the corroboration it has.
 *
 * Read-only. A source is opened in a browser tab or it is not; there is nothing to decide
 * here, and the corpus passages have nowhere to be opened to.
 */

import { For, Show, type JSX } from "solid-js";
import { EmptyState, Text } from "~/ui";
import { relativeTime } from "~/lib/format";
import {
  BUCKET_HINT,
  BUCKET_LABEL,
  type SourceInventory,
} from "../viewport/sourceItems";
import { SourceRow } from "./SourceRow";
import { SurfaceBody, SurfaceSection, SurfaceSummary } from "./surfaceChrome";

export function SourcesSurface(props: {
  inventory: () => SourceInventory;
}): JSX.Element {
  const inv = () => props.inventory();
  return (
    <SurfaceBody>
      <Show
        when={inv().items.length}
        fallback={
          <EmptyState
            icon="search"
            message="No sources yet"
            hint="What this thread reads — the web and your knowledge base — lands here."
          />
        }
      >
        <InventoryHead inventory={inv()} />
        <For each={inv().groups}>
          {(group) => (
            <SurfaceSection
              label={BUCKET_LABEL[group.bucket]}
              count={group.items.length}
              hint={BUCKET_HINT[group.bucket]}
              gap={1}
              columns
            >
              <For each={group.items}>
                {(item) => <SourceRow item={item} />}
              </For>
            </SurfaceSection>
          )}
        </For>
      </Show>
    </SurfaceBody>
  );
}

/** When the inventory was last added to.
 *
 *  The two figures it is read against — sources, and the independent origins behind
 *  them — ride the pane header (`SURFACE_META.sources`), which is there whether the pane
 *  is a leaf or a tab. Printing them here as well put the same count twice in one inch.
 */
function InventoryHead(props: { inventory: SourceInventory }): JSX.Element {
  return (
    <Show when={props.inventory.newestRetrieval}>
      {(at) => (
        <SurfaceSummary>
          <Text variant="micro" tone="dim">
            last read {relativeTime(at())}
          </Text>
        </SurfaceSummary>
      )}
    </Show>
  );
}
