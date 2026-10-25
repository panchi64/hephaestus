import { Show, type JSX } from "solid-js";
import { Text } from "~/ui";

/** The rail's chrome: frosted column, the wordmark plate on top, a body that owns the
 *  scroll, and a footer ruled off below. The app sidebar and the onboarding checklist
 *  both stand in it, so the walkthrough reads as the same instrument before the shell
 *  exists. */
export function RailFrame(props: {
  /** Makes the wordmark a link; omitted where there is nowhere to go yet. */
  brandHref?: string;
  /** The dim id beside the wordmark. */
  assetId: string;
  children: JSX.Element;
  footer?: JSX.Element;
}): JSX.Element {
  const brand = (): JSX.Element => (
    <>
      <span class="heph-rail-wordmark">Hephaestus</span>
      <Text variant="micro" tone="dim">
        {props.assetId}
      </Text>
    </>
  );
  return (
    <nav class="heph-rail heph-rail-glass flex h-full min-h-0 flex-col">
      <div class="sticky top-0 z-30">
        {/* The wordmark in the machine's voice, wide-tracked, with the workspace id
            dim beside it. It stacks under the brand at the rail's narrowest. */}
        <Show
          when={props.brandHref}
          fallback={<div class="heph-rail-brand">{brand()}</div>}
        >
          {(href) => (
            <a href={href()} class="heph-rail-brand hover:bg-raised">
              {brand()}
            </a>
          )}
        </Show>
      </div>
      {props.children}
      <Show when={props.footer}>
        <div class="sticky bottom-0 border-t border-line-strong">
          {props.footer}
        </div>
      </Show>
    </nav>
  );
}
