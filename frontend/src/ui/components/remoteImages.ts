import { api } from "~/lib/api";
import { hostLabel } from "~/lib/format";

/**
 * Resolve the images an answer embedded, without the operator's browser ever
 * contacting the host that serves them.
 *
 * `markedLinks` renders a remote image as an `<img>` with **no `src`** and its
 * address in `data-remote-src`, precisely so nothing loads on parse. This pass
 * walks that markup and fills the `src` in from `/media/remote-image`, which
 * fetches the bytes backend-side and validates them (services/webimage.py). The
 * request that leaves this machine's browser is same-origin; the one that reaches
 * the remote host comes from the backend and carries no operator identity.
 *
 * It is a DOM pass rather than a component because the prose around it is parsed
 * markdown injected as HTML — there is no JSX seam to hang a component off. It is
 * idempotent (an `<img>` that already has a `src` is skipped), which is what makes
 * it safe to re-run on every streaming delta.
 */

/** Resolved object URLs by remote address, shared across every Markdown instance.
 *
 *  This cache is what makes the pass safe to run mid-stream: the trailing block
 *  re-parses on *every* delta, so an image in it would otherwise be re-fetched
 *  dozens of times and flash back to empty on each one. Keyed by the remote URL,
 *  so the same picture referenced twice resolves once.
 *
 *  Capped, and eviction revokes — an object URL pins its bytes in memory until it
 *  does, and these are images. Oldest-first (Map preserves insertion order),
 *  matching the block parse cache next door. */
const RESOLVED_CAP = 32;
const resolved = new Map<string, Promise<string>>();

function proxyPath(remote: string): string {
  return `/media/remote-image?url=${encodeURIComponent(remote)}`;
}

function resolveOnce(remote: string): Promise<string> {
  const hit = resolved.get(remote);
  if (hit !== undefined) return hit;
  const pending = api
    .getBlob(proxyPath(remote))
    .then((blob) => URL.createObjectURL(blob));
  // A failed fetch must not be cached as a permanent "no": the operator may open
  // the thread again on a working link, and a transient 502 should not poison the
  // image for the session.
  pending.catch(() => resolved.delete(remote));
  resolved.set(remote, pending);
  if (resolved.size > RESOLVED_CAP) {
    const oldest = resolved.keys().next().value;
    if (oldest !== undefined && oldest !== remote) {
      const stale = resolved.get(oldest);
      resolved.delete(oldest);
      void stale?.then((url) => URL.revokeObjectURL(url)).catch(() => {});
    }
  }
  return pending;
}

/** The honest stand-in for an image that would not load: the alt text if the model
 *  wrote one, and the host either way, so the operator can see what was meant and
 *  where it was meant to come from. A broken-image icon says neither. */
function replaceWithCaption(img: HTMLImageElement, remote: string): void {
  const caption = document.createElement("span");
  caption.dataset.remoteImageError = "";
  caption.className = "heph-remote-image-error";
  const alt = img.getAttribute("alt")?.trim();
  caption.textContent = alt
    ? `${alt} — image unavailable (${hostLabel(remote)})`
    : `Image unavailable (${hostLabel(remote)})`;
  img.replaceWith(caption);
}

/**
 * Fill in the `src` of every unresolved remote image under `root`.
 *
 * Marks each element before awaiting, so a re-run mid-flight (the next streaming
 * delta) doesn't start a second fetch for the same node.
 */
export function hydrateRemoteImages(root: HTMLElement): void {
  const pending = root.querySelectorAll<HTMLImageElement>(
    "img[data-remote-src]:not([data-remote-state])",
  );
  pending.forEach((img) => {
    const remote = img.dataset.remoteSrc;
    if (!remote) return;
    img.dataset.remoteState = "loading";
    void resolveOnce(remote).then(
      (objectUrl) => {
        // The node can be gone by now — a streaming delta re-parsed the block it
        // lived in. Setting `src` on a detached image is harmless but pointless.
        if (!img.isConnected) return;
        img.src = objectUrl;
        img.dataset.remoteState = "ready";
      },
      () => {
        if (!img.isConnected) return;
        replaceWithCaption(img, remote);
      },
    );
  });
}
