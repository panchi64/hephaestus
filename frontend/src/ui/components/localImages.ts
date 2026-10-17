/** Resolves a local image reference (`screenshot:<id>` → `<id>`) to a `src`, or
 *  `undefined` when this view does not hold it. */
export type LocalImageResolver = (ref: string) => string | undefined;

const LOCAL_IMAGE = /^screenshot:([\w.:-]+)$/;

/** The reference in a `screenshot:<id>` address, or null for any other address. */
export function localImageRef(href: string): string | null {
  return LOCAL_IMAGE.exec(href.trim())?.[1] ?? null;
}

/**
 * Fill in the `src` of every unresolved local image under `root`.
 *
 * `markdownLinks` parks the reference in `data-local-ref` so a multi-megabyte data URI
 * never enters the parse cache. One the resolver can't find (no resolver, or a
 * screenshot this view no longer holds) becomes its caption, styled like a remote image
 * that would not load.
 */
export function hydrateLocalImages(
  root: HTMLElement,
  resolve: LocalImageResolver | undefined,
): void {
  const pending = root.querySelectorAll<HTMLImageElement>(
    "img[data-local-ref]:not([src])",
  );
  pending.forEach((img) => {
    const src = resolve?.(img.dataset.localRef ?? "");
    if (src !== undefined) {
      img.src = src;
      return;
    }
    const caption = document.createElement("span");
    caption.className = "heph-remote-image-error";
    const alt = img.getAttribute("alt")?.trim();
    caption.textContent = alt
      ? `${alt} — screenshot unavailable`
      : "Screenshot unavailable";
    img.replaceWith(caption);
  });
}
