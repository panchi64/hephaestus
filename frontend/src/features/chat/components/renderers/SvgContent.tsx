/** Renders an SVG artifact as an `<img>` over a re-tagged object URL — an `<img>`
 *  cannot execute embedded script, which is the whole point: SVG bytes are never
 *  `innerHTML`-ed or framed (that would let a model-authored file run as the
 *  operator). Clicking opens the shared Lightbox, matching how inline image
 *  attachments open (`MessageAttachments`). Presentation-only. */

import {
  createResource,
  createSignal,
  onCleanup,
  Show,
  type JSX,
} from "solid-js";
import { ErrorState, Lightbox, LoadingText } from "~/ui";
import { rememberScroll } from "../../scrollMemory";
import { settled } from "~/lib/resource";

async function toSvgObjectUrl(blob: Blob): Promise<string> {
  const bytes = await blob.arrayBuffer();
  return URL.createObjectURL(new Blob([bytes], { type: "image/svg+xml" }));
}

export function SvgContent(props: {
  data: Blob;
  name: string;
  fontStep?: number;
  softWrap?: boolean;
  scrollKey?: string;
}): JSX.Element {
  const [url] = createResource(() => props.data, toSvgObjectUrl);
  onCleanup(() => {
    const u = url();
    if (u) URL.revokeObjectURL(u);
  });

  const [lightboxOpen, setLightboxOpen] = createSignal(false);
  const scrollKey = (): string => props.scrollKey ?? props.name;

  return (
    <Show
      when={!url.error}
      fallback={<ErrorState message="Could not load this file." />}
    >
      <Show when={settled(url)} fallback={<LoadingText />}>
        {(u) => (
          <div
            ref={(el) => rememberScroll(el, scrollKey)}
            class="h-full overflow-auto p-3"
          >
            {/* Bare inside the pane's frame (design §8): the image on the region's own
                ground, no card lifted around it. */}
            <button
              type="button"
              onClick={() => setLightboxOpen(true)}
              aria-label={`View ${props.name}`}
              class="flex h-full w-full items-center justify-center"
            >
              <img
                src={u()}
                alt={props.name}
                class="h-full w-full object-contain"
              />
            </button>
            <Lightbox
              items={[{ src: u(), filename: props.name }]}
              index={0}
              open={lightboxOpen()}
              onClose={() => setLightboxOpen(false)}
              onNavigate={() => {}}
            />
          </div>
        )}
      </Show>
    </Show>
  );
}
