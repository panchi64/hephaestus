import {
  createEffect,
  createMemo,
  createResource,
  Match,
  Switch,
  type JSX,
  type Resource,
} from "solid-js";
import { api, useAuthedBlobUrl } from "~/lib/api";
import { EmptyState, ErrorState, LoadingText } from "~/ui";
import { snapshotFilePath } from "../data";
import type { SnapshotFile, ViewSnapshotRef } from "../model";
import { pickEntryHtml } from "../viewport/viewItems";
import { createDownloadSlot } from "../viewport/downloadRegistry";
import { SandboxedFrame } from "./SandboxedFrame";
import { settled } from "~/lib/resource";

/**
 * Renders a workspace snapshot's PREVIEW — its entry HTML page (index.html, else the
 * first HTML file) rendered in an opaque-origin sandboxed iframe. A snapshot is a
 * static capture, not a live server, so assets resolve from the captured bytes. When
 * the snapshot has no HTML page there is nothing to render; the operator uses CODE to
 * browse its files instead. The file list is owned by the stage and passed in, so
 * flipping PREVIEW/CODE doesn't refetch it.
 */
export function ViewSnapshotPreview(props: {
  snapshot: ViewSnapshotRef;
  files: Resource<SnapshotFile[]>;
  /** Retries the owning stage's file-list fetch — armed on the `files` resource's
   *  own refetch, so a failed list load can be retried in place. */
  onRetryFiles?: () => void;
  /** Manual reload nonce — bumping it reloads the framed entry page in place. */
  reloadKey: number;
  /** Operator zoom step (-2..+2), applied as whole-page scale on the frame. */
  fontStep?: number;
}): JSX.Element {
  const entry = createMemo(() => {
    // `latest`, not the resource itself: reading a resource mid-fetch inside a
    // tracked scope suspends the nearest `Suspense` boundary, and this one's is
    // the pane's — so a list refetch would blank the pane it is rendering into
    // rather than leaving the page on screen until the new list lands.
    const list = settled(props.files);
    return list ? pickEntryHtml(list) : undefined;
  });
  const renderUrl = useAuthedBlobUrl(() => {
    const path = entry();
    return path ? snapshotFilePath(props.snapshot.snapshotId, path) : undefined;
  });

  // Arm the panel-level download button with the auto-picked entry page's raw
  // bytes — the blob-URL hook above only exposes an object URL (for the iframe
  // `src`), so this fetches the same content separately, once per entry path.
  const [entryBlob] = createResource(
    () => {
      const path = entry();
      return path ? ([props.snapshot.snapshotId, path] as const) : undefined;
    },
    ([snapshotId, path]) => api.getBlob(snapshotFilePath(snapshotId, path)),
  );
  const armDownload = createDownloadSlot();
  createEffect(() => {
    const path = entry();
    const blob = entryBlob();
    if (!path || !blob) return;
    armDownload({ name: path, getBlob: async () => blob });
  });

  return (
    <Switch fallback={<LoadingText label="Loading preview" />}>
      <Match when={props.files.error}>
        <ErrorState
          message="Could not load this version."
          onRetry={props.onRetryFiles}
        />
      </Match>
      <Match when={settled(props.files) && !entry()}>
        <EmptyState
          message="No preview"
          hint="This version has no HTML page — use CODE to browse its files."
        />
      </Match>
      <Match when={renderUrl.error}>
        <ErrorState message="Could not render this version." />
      </Match>
      <Match when={settled(renderUrl)}>
        {(url) => (
          <SandboxedFrame
            src={url()}
            title={entry()!}
            reloadKey={props.reloadKey}
            fontStep={props.fontStep}
          />
        )}
      </Match>
    </Switch>
  );
}
