import { Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { RegistrationFrame } from "./RegistrationFrame";

export interface PageHeaderProps {
  /** Section title (rendered in the display face). */
  title: string;
  /** Dim subtitle / description under the title. */
  subtitle?: string;
  /** Diegetic asset/version ID shown above the title (e.g. "HEPH-HUD-00.1"). */
  assetId?: string;
  /** Right-aligned actions (buttons, status flags). */
  actions?: JSX.Element;
  /** `page` (default) opens a routed screen: a display-size `h1`, the asset
   *  plate, and the registration marks around it. `section` names one region
   *  *inside* something else — a settings dialog's pane — where all three would
   *  be wrong: a second display title competes with the dialog's own, and a
   *  second set of marks sits inside the frame the overlay already drew. */
  variant?: "page" | "section";
  /** Draw registration marks at the four corners of the header row. Default
   *  true — see the note on the component. */
  framed?: boolean;
  class?: string;
}

/** Standard screen header: every feature screen opens with one. The single
 *  per-screen `display` title lives here.
 *
 *  **The registration marks frame this row, not the viewport.** They used to sit
 *  on the shell's content region, which put two of them just above the page and
 *  the other two in the bottom corners of the screen — framing the window rather
 *  than anything in it, which is the one thing a registration mark is for. Here
 *  they bracket a real object: the asset id, title, subtitle and status badge,
 *  read as one plate. The diegetic detail is retained (§11) and finally points
 *  at something. */
export function PageHeader(props: PageHeaderProps): JSX.Element {
  const [local] = splitProps(props, [
    "title",
    "subtitle",
    "assetId",
    "actions",
    "framed",
    "variant",
    "class",
  ]);
  const section = (): boolean => local.variant === "section";

  const row = (
    /* Inset far enough to clear the 10px corner marks at their 4px offset —
       without it the asset id collides with the top-left cross. */
    <div
      class={cx(
        "flex flex-wrap items-end justify-between gap-4",
        section() ? "px-1" : "px-5 py-4",
      )}
    >
      <div class="flex flex-col gap-1">
        {/* Diegetic asset id (§11) — machine output, so it takes the mono
            voice and sits above the title as an eyebrow, small and dim enough
            that the eye skips it until it wants it. */}
        <Show when={local.assetId && !section()}>
          <Text variant="meta" tone="dim">
            {local.assetId}
          </Text>
        </Show>
        <Text
          variant={section() ? "readout-lg" : "display"}
          tone="bright"
          as={section() ? "h2" : "h1"}
        >
          {local.title}
        </Text>
        <Show when={local.subtitle}>
          <Text variant="body" tone="dim">
            {local.subtitle}
          </Text>
        </Show>
      </div>
      <Show when={local.actions}>
        <div class="flex items-center gap-2">{local.actions}</div>
      </Show>
    </div>
  );

  return (
    /* No rule under the title (§7). A display-size heading with air beneath it
       already separates itself from the page. */
    <header class={cx(section() ? "pb-3" : "pb-6", local.class)}>
      <Show when={local.framed !== false && !section()} fallback={row}>
        <RegistrationFrame>{row}</RegistrationFrame>
      </Show>
    </header>
  );
}
