import { Show, type JSX } from "solid-js";
import { cx } from "~/ui";
import { frameZoom } from "./renderers/fontStep";

/**
 * The single mount point for model-generated markup — a static HTML version, the
 * live View head, or a plugin's window. Deliberately sandboxed **without**
 * `allow-same-origin`, so the framed content runs in an opaque origin and can't read
 * the operator's cookies or act as the operator against the API. Keep this the only
 * place the `sandbox` value lives so that security contract can't drift between render
 * paths.
 *
 * A `src` change reloads the frame natively (a new live URL, a switched version). A
 * manual reload of the *same* src is driven by `reloadKey`: bumping it remounts only
 * the inner iframe (keyed on `src` + `reloadKey`), so a refresh reloads the page in
 * place without disturbing the surrounding stage. This stays a thin frame.
 *
 * The panel's A-/A+ control (`fontStep`) scales the whole page (`frameZoom`), like
 * browser zoom — the opaque origin means no class or font-size can reach the
 * document inside. The scale is an inline style, not part of the key, so stepping
 * the zoom never reloads the page.
 */
export function SandboxedFrame(props: {
  src: string;
  title: string;
  class?: string;
  reloadKey?: number;
  /** Operator zoom step (-2..+2), rendered as a whole-page scale. */
  fontStep?: number;
  /** `plugin` frames a plugin's window: scripts only, no forms or popups, since its
   *  one way out is the message port the host hands it. */
  policy?: "view" | "plugin";
  onLoad?: (frame: HTMLIFrameElement) => void;
}): JSX.Element {
  const zoom = (): number => frameZoom(props.fontStep);
  return (
    <Show keyed when={`${props.src}#${props.reloadKey ?? 0}`}>
      <iframe
        src={props.src}
        title={props.title}
        class={cx("h-full w-full border-0 bg-bright", props.class)}
        style={
          zoom() === 1
            ? undefined
            : {
                // Compensated size × scale = exactly the parent's box, so the
                // zoomed frame neither spills nor leaves a gap.
                width: `${100 / zoom()}%`,
                height: `${100 / zoom()}%`,
                transform: `scale(${zoom()})`,
                "transform-origin": "0 0",
              }
        }
        sandbox={
          props.policy === "plugin"
            ? "allow-scripts"
            : "allow-scripts allow-forms allow-popups"
        }
        onLoad={(event) => props.onLoad?.(event.currentTarget)}
      />
    </Show>
  );
}
