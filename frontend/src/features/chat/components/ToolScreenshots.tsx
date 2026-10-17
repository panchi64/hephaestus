import { For, createSignal, type JSX } from "solid-js";
import { Lightbox } from "~/ui";
import type { ToolImage } from "../model";

/** What a call saw, as thumbnails on its row that open full screen. On the row rather
 *  than behind the disclosure, so a screenshot is never a click away from being seen. */
export function ToolScreenshots(props: {
  images: ToolImage[];
  toolName: string;
}): JSX.Element {
  const [index, setIndex] = createSignal<number | null>(null);
  const src = (image: ToolImage): string =>
    `data:${image.mediaType};base64,${image.data}`;
  const label = (i: number): string =>
    `What ${props.toolName} saw${props.images.length > 1 ? `, ${i + 1}` : ""}`;
  return (
    <div class="flex flex-wrap gap-2 px-2 pb-2">
      <For each={props.images}>
        {(image, i) => (
          <button
            type="button"
            class="block cursor-zoom-in"
            aria-label={`View ${label(i()).toLowerCase()}`}
            onClick={() => setIndex(i())}
          >
            <img
              /* Base64 is in the DOM already, so there is nothing for `lazy` to defer;
                 `async` keeps decoding dozens of them off the frame a turn lands on. */
              decoding="async"
              class="block max-h-24 max-w-40 border border-line"
              src={src(image)}
              alt={label(i())}
            />
          </button>
        )}
      </For>
      <Lightbox
        items={props.images.map((image, i) => ({
          src: src(image),
          filename: label(i),
        }))}
        index={index() ?? 0}
        open={index() !== null}
        onClose={() => setIndex(null)}
        onNavigate={setIndex}
      />
    </div>
  );
}
