import { type JSX } from "solid-js";
import { RegistrationFrame, Text } from "~/ui";

/** Bare, centered layout for unauthenticated surfaces (login, signup). No
 *  sidebar — just the framed brand and a narrow content column. */
export function AuthLayout(props: { children: JSX.Element }): JSX.Element {
  return (
    <div class="flex h-screen items-center justify-center text-text">
      <RegistrationFrame class="w-full max-w-sm p-8" assetId="HEPH-AUTH-01.0">
        <div class="mb-6 flex flex-col gap-0.5">
          <Text variant="display" tone="bright">
            Hephaestus
          </Text>
          <Text variant="micro" tone="dim">
            SECURE WORKSPACE · AUTHORIZED ACCESS ONLY
          </Text>
        </div>
        {props.children}
      </RegistrationFrame>
    </div>
  );
}
