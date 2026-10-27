import { type JSX } from "solid-js";
import type { AssetId } from "../assetIds";
import { Text } from "../primitives/Text";
import { ConstructionReveal } from "./ConstructionReveal";

export interface PlateProps {
  /** Diegetic id engraved at the plate's foot. */
  assetId: AssetId;
  /** Width glue for the plate. */
  class?: string;
  /** Layout of the content column, padding included. */
  contentClass?: string;
  children: JSX.Element;
}

/**
 * A standalone screen's framed glass plate: it assembles itself on mount (§8, the
 * construction reveal) and never closes, because the screen it fronts has no state
 * in which it does.
 */
export function Plate(props: PlateProps): JSX.Element {
  return (
    <ConstructionReveal
      when
      origin="top-left"
      // NOT `isolate` and no `z-index`: either makes this a backdrop root and the
      // glass would frost nothing but itself (see `.heph-glass`).
      class={props.class}
      contentClass={props.contentClass}
    >
      {props.children}
      {/* In flow rather than pinned to the bottom edge: the frame's rules sit 6px
          inside the region, and a line on the region's edge would cross one. */}
      <Text variant="plate" tone="dim" class="mt-2 self-center">
        {props.assetId}
      </Text>
    </ConstructionReveal>
  );
}
