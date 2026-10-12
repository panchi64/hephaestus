import { For, Show, type JSX } from "solid-js";
import { Button, Tooltip } from "~/ui";
import type { ChatViewport } from "../useChatViewport";
import { SURFACES } from "../viewport/surfaces";

/**
 * One button per surface that has something to show.
 *
 * It replaces the single eye toggle, which could only say "the panel" — fine while the
 * panel held one thing, and a guess the moment it holds several. A button per surface
 * says which, and clicking it puts that surface on screen rather than restoring whatever
 * was there last.
 *
 * **Only available surfaces render.** A button for a surface with nothing behind it is a
 * button that opens onto an empty frame, and the operator learns to distrust the row. In
 * an ordinary chat thread that leaves one button or none; a worktree thread with a plan and a
 * diff shows three. The row is therefore usually shorter than the registry, which is what
 * keeps it from crowding a header that also carries the title, the workspace hint, the
 * branch chip and the session menu.
 *
 * `md` rather than the product's standard `sm` ghost icon, matching the session-actions
 * trigger beside it: these are the most-reached-for controls in the header and are sized
 * as peers. That was the eye's reasoning and it survives the eye.
 */
export function ViewportSurfaceBar(props: {
  viewport: ChatViewport;
}): JSX.Element {
  // The leftmost button actually rendered is `viewport.firstAvailable()` — the row is a
  // filtered view of the registry, so the *registry's* first surface is usually not in it
  // (`plan` rarely is), and keying focus-return on its index left the common thread with
  // no return target at all. Read off the facade rather than re-derived here, because it
  // is also what an empty panel opens onto: those two must name the same surface.
  return (
    <For each={SURFACES}>
      {(spec) => (
        <Show when={props.viewport.available(spec.id)}>
          <Tooltip label={spec.label} side="bottom">
            <Button
              // Where focus returns when the full-screen sheet closes. Any of
              // them would do; the leftmost is the one that is there whenever
              // the row is.
              ref={(el) => {
                if (spec.id === props.viewport.firstAvailable())
                  props.viewport.triggerRef(el);
              }}
              variant="ghost"
              leading={spec.icon}
              aria-label={`Toggle ${spec.label} surface`}
              aria-pressed={props.viewport.isOpen(spec.id)}
              active={props.viewport.isOpen(spec.id)}
              onClick={() => props.viewport.toggleSurface(spec.id)}
            >
              {/* The unseen badge is the View's alone for now — every other
                  surface's notion of "new" is its own, and inventing one here
                  would be this component deciding something it does not know. */}
              <Show
                when={spec.id === "view" && props.viewport.unseenCount() > 0}
              >
                {props.viewport.unseenCount() > 9
                  ? "9+"
                  : props.viewport.unseenCount()}
              </Show>
            </Button>
          </Tooltip>
        </Show>
      )}
    </For>
  );
}
