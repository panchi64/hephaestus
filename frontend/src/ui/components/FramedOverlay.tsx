import { Show, createEffect, onCleanup, splitProps, type JSX } from "solid-js";
import { Portal } from "solid-js/web";
import { cx } from "../cx";
import { ConstructionReveal } from "./ConstructionReveal";

export interface FramedOverlayProps {
  open: boolean;
  onClose: () => void;
  /** Sizing for the dialog itself — width, max-width, height. Layout glue only;
   *  never a `bg-*`, see the note on the backdrop root below. */
  class?: string;
  /** Layout glue for the element holding the children, between the frame and the
   *  content — where a caller laying its children out with flex puts that flex. */
  contentClass?: string;
  /** Id of the element naming this dialog, for `aria-labelledby`. */
  labelledBy?: string;
  /** Which corner the frame is drawn from. Default `top-left`: a centered overlay
   *  belongs to the page rather than to an edge, and the reading order starts
   *  there. The View passes `top-right` because it arrives from that screen edge. */
  origin?: "top-right" | "top-left";
  children: JSX.Element;
}

/**
 * **A centered overlay that builds its own frame.** The `ConstructionReveal`
 * container the View panel already uses, pinned to the middle of the viewport
 * over a dimmed page: the `+` splits from the origin corner, travels the top
 * edge, drops down the sides, and the glass resolves inside the frame it just
 * described. Closing takes it apart again.
 *
 * It exists so an overlay that wants that arrival — the settings dialog — does
 * not hand-roll the portal, the backdrop, the escape key and the four traps
 * below, which would drift from any second copy within a release.
 *
 * **This is not `Modal`, and could not be built on it.** `Modal`'s backdrop is
 * the *ancestor* of its dialog, which is fine for an opaque card and fatal here:
 * see the backdrop-root note below.
 *
 * Four things it is easy to break, every one of which fails silently — the page
 * still renders, it just quietly stops being glass:
 *
 * - **The backdrop is a SIBLING of the reveal, never its parent.** `backdrop-filter`
 *   only blurs what is painted inside its *backdrop root*, and `opacity < 1` on any
 *   ancestor creates one — which the backdrop's own fade animation does for as long
 *   as it runs. Nest the reveal inside it and `.heph-glass` blurs nothing while its
 *   translucent fill still tints, so the result reads as a slightly lighter panel
 *   rather than as anything broken. Same reason `ConstructionReveal`'s wrapper is
 *   `relative` and not `relative isolate`.
 * - **The dialog carries no fill.** The frosted surface *is* the framed region the
 *   reveal draws; a `bg-*` here stacks a second layer over it and paints the page
 *   behind out. Anything inside is `Panel bare`.
 * - **The centering layer spans the viewport but takes no clicks** — it has to be
 *   full-size for `place-items-center` to center against the viewport, so it is
 *   `pointer-events-none` and the reveal re-enables them. Without that it would
 *   cover the backdrop and swallow every dismissing click.
 * - **Centering goes on that layer; the reveal is sized to the dialog.** The frame
 *   is drawn on the reveal's own wrapper, so pinning *that* to the viewport puts
 *   the corner marks in the corners of the screen — which is what the View's
 *   full-screen sheet wants, and wrong for a dialog.
 */
export function FramedOverlay(props: FramedOverlayProps): JSX.Element {
  const [local] = splitProps(props, [
    "open",
    "onClose",
    "class",
    "contentClass",
    "labelledBy",
    "origin",
    "children",
  ]);

  // Escape closes, matching every other overlay in the system (Lightbox,
  // Popover, Menu). Bound while open and torn down on close, so a stack of
  // overlays never leaves a listener behind that closes the wrong one.
  createEffect(() => {
    if (!local.open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") local.onClose();
    };
    window.addEventListener("keydown", onKey);
    onCleanup(() => window.removeEventListener("keydown", onKey));
  });

  return (
    <Portal>
      {/* The dim behind the glass. Its own element, mounted and unmounted on
          `open` — the reveal outlives `open` by the length of its exit, and the
          backdrop should not, or the page stays dimmed while the frame is being
          taken apart. */}
      <Show when={local.open}>
        <div
          class="heph-fade-in fixed inset-0 z-50 bg-bg/70"
          aria-hidden="true"
          onClick={local.onClose}
        />
      </Show>
      {/* Centering happens HERE, on a viewport-sized layer, and never on the
          reveal. The frame is drawn on the reveal's own wrapper, so a wrapper
          pinned to the viewport would put the corner marks in the corners of the
          *screen* — which is exactly what the chat View's full-screen sheet
          wants, and exactly wrong for a dialog. The reveal is therefore sized to
          the dialog, and this layer positions it.

          Always mounted: it is empty and takes no clicks when closed, and
          gating it would tear the reveal out before it could play its exit. */}
      <div class="pointer-events-none fixed inset-0 z-50 grid place-items-center p-6">
        <ConstructionReveal
          when={local.open}
          origin={local.origin ?? "top-left"}
          class={cx(
            "pointer-events-auto flex max-h-[85vh] min-h-0 w-full flex-col",
            local.class,
          )}
          contentClass={cx("flex min-h-0 flex-1 flex-col", local.contentClass)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby={local.labelledBy}
            class="flex min-h-0 flex-1 flex-col"
          >
            {local.children}
          </div>
        </ConstructionReveal>
      </div>
    </Portal>
  );
}
