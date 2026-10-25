import { type JSX } from "solid-js";
import { Plate, Text } from "~/ui";

/** Bare, centered layout for unauthenticated surfaces (login, signup). No
 *  sidebar — just the brand on a plate that builds its own frame and a narrow
 *  content column. */
export function AuthLayout(props: { children: JSX.Element }): JSX.Element {
  return (
    <div class="flex h-screen items-center justify-center text-text">
      <Plate
        assetId="HEPH-AUTH-01.0"
        class="w-full max-w-sm"
        contentClass="flex flex-col gap-4 p-8"
      >
        <div class="mb-2 flex flex-col gap-0.5">
          <Text variant="display" tone="bright">
            Hephaestus
          </Text>
          <Text variant="micro" tone="dim">
            SECURE WORKSPACE · AUTHORIZED ACCESS ONLY
          </Text>
        </div>
        {props.children}
      </Plate>
    </div>
  );
}
