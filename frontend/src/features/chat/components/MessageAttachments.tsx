import { createMemo, createSignal, For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  AttachmentChip,
  Icon,
  ImageFrame,
  Lightbox,
  LoadingText,
  type LightboxItem,
} from "~/ui";
import { useAuthedBlobUrl } from "~/lib/api";
import { useUploads } from "~/features/uploads/data";
import type { Upload } from "~/features/uploads/model";

interface MessageAttachmentsProps {
  /** Upload ids attached to this sent user turn. */
  ids: string[];
}

const isImage = (u: Upload | undefined): boolean =>
  (u?.mime ?? "").startsWith("image/");

/** A single inline image preview at its own aspect. Resolves its preview bytes through the
 *  auth-gated path (an `<img>` can't carry a bearer); clicking opens the shared
 *  Lightbox over the turn's images. Carries the KB-excluded marker so a withheld
 *  image still reads as withheld from the thread (matching the chip it replaced). */
function ImageThumb(props: {
  id: string;
  name: string;
  kbExcluded: boolean;
  onOpen: () => void;
}): JSX.Element {
  const src = useAuthedBlobUrl(() => `/uploads/${props.id}/thumbnail?size=512`);
  return (
    <button
      type="button"
      onClick={props.onOpen}
      aria-label={`View ${props.name}`}
      class="relative block cursor-pointer"
    >
      <ImageFrame
        src={src()}
        error={src.error !== undefined}
        alt={props.name}
        fit="natural"
        class="max-h-80 max-w-80 min-h-24 min-w-24"
      />
      <Show when={props.kbExcluded}>
        <span
          class="absolute right-1 top-1 text-warn"
          title="Excluded from knowledge base"
          aria-label="Excluded from knowledge base"
        >
          <Icon name="database" size={12} />
        </span>
      </Show>
    </button>
  );
}

/** Read-only attachments on a sent user turn. Resolves each id against the uploads
 *  seam (the single source of truth — current KB membership, not a stale copy):
 *  image uploads render as inline previews opening a shared Lightbox, everything
 *  else as a filename chip linking to the file (a since-deleted upload reads as an
 *  error chip). Until the seam resolves nothing is classified — otherwise every id,
 *  images included, would briefly look "missing" and flash as error chips. */
export function MessageAttachments(
  props: MessageAttachmentsProps,
): JSX.Element {
  const uploads = useUploads();
  // One keyed index rebuilt per list change — O(1) lookups across every item,
  // not a linear scan on every reactive update.
  const byId = createMemo(
    () =>
      new Map<string, Upload>((settled(uploads) ?? []).map((u) => [u.id, u])),
  );
  // The shared uploads list hasn't resolved yet (no rows, still loading): hold off
  // classifying so a valid image isn't mistaken for a missing file mid-load.
  const resolving = (): boolean =>
    uploads.loading && settled(uploads) === undefined;

  // Partition by media type: images get inline previews, the rest stay chips.
  const images = createMemo(() =>
    props.ids.filter((id) => isImage(byId().get(id))),
  );
  const others = createMemo(() =>
    props.ids.filter((id) => !isImage(byId().get(id))),
  );

  // Lightbox over this turn's images; null = closed. Only the active item's
  // full-size bytes are fetched (tiles use thumbnails) — re-resolved on navigate.
  const [lightbox, setLightbox] = createSignal<number | null>(null);
  const activeId = () => {
    const i = lightbox();
    return i === null ? undefined : images()[i];
  };
  const activeSrc = useAuthedBlobUrl(() => {
    const id = activeId();
    return id ? `/uploads/${id}/content` : undefined;
  });
  const items = createMemo<LightboxItem[]>(() =>
    images().map((id, i) => ({
      src: i === lightbox() ? activeSrc() : undefined,
      error: i === lightbox() ? activeSrc.error !== undefined : false,
      filename: byId().get(id)?.name ?? id,
    })),
  );

  return (
    <Show when={props.ids.length > 0}>
      <Show
        when={!resolving()}
        fallback={
          <div class="flex max-w-[80%] justify-end">
            <LoadingText label="Loading attachments" />
          </div>
        }
      >
        <div class="flex max-w-[80%] flex-wrap items-end justify-end gap-2">
          <For each={others()}>
            {(id) => {
              const u = () => byId().get(id);
              return (
                <AttachmentChip
                  name={u()?.name ?? id}
                  status={u() ? undefined : "error"}
                  kbExcluded={u()?.kbExcluded ?? false}
                />
              );
            }}
          </For>
          <For each={images()}>
            {(id, i) => (
              <ImageThumb
                id={id}
                name={byId().get(id)?.name ?? id}
                kbExcluded={byId().get(id)?.kbExcluded ?? false}
                onOpen={() => setLightbox(i())}
              />
            )}
          </For>
        </div>
        <Lightbox
          items={items()}
          index={lightbox() ?? 0}
          open={lightbox() !== null}
          onClose={() => setLightbox(null)}
          onNavigate={setLightbox}
        />
      </Show>
    </Show>
  );
}
