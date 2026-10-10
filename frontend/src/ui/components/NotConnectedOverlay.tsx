import type { JSX } from "solid-js";
import { Text } from "../primitives/Text";

/**
 * Marks a surface whose backend capability does not exist yet. It sits over the
 * (still-rendered) mock screen so the design is visible behind it, framed in a
 * 2px alert border with a large NOT CONNECTED readout. It captures pointer events
 * so the inert screen can't be interacted with — `accent-alert` here means
 * exactly one thing: this capability is absent (design system §4).
 */
export function NotConnectedOverlay(): JSX.Element {
  return (
    <div class="pointer-events-auto absolute inset-0 z-30 flex flex-col items-center justify-center gap-2 border-2 border-alert bg-bg/70">
      <Text variant="display" tone="alert">
        Not connected
      </Text>
      <Text variant="micro" tone="dim">
        Awaiting backend capability
      </Text>
    </div>
  );
}
