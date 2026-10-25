/** Renders a PDF artifact page-by-page via `pdfjs-dist`, fit-width into a canvas.
 *  The document and its worker are loaded lazily (dynamic import) so the ~1MB
 *  pdf.js runtime only ships to conversations that actually view a PDF. */

import {
  createEffect,
  createResource,
  createSignal,
  onCleanup,
  Show,
  type JSX,
} from "solid-js";
import type {
  PDFDocumentLoadingTask,
  RenderTask,
} from "pdfjs-dist/types/src/display/api";
import { Button, ErrorState, LoadingText, Text } from "~/ui";
import { settled } from "~/lib/resource";

export function PdfViewer(props: {
  data: Blob;
  name: string;
  fontStep?: number;
  softWrap?: boolean;
  scrollKey?: string;
}): JSX.Element {
  let loadingTask: PDFDocumentLoadingTask | undefined;
  const [doc] = createResource(
    () => props.data,
    async (data) => {
      const pdfjs = await import("pdfjs-dist");
      pdfjs.GlobalWorkerOptions.workerSrc = new URL(
        "pdfjs-dist/build/pdf.worker.min.mjs",
        import.meta.url,
      ).toString();
      const buffer = await data.arrayBuffer();
      loadingTask = pdfjs.getDocument({ data: buffer });
      return loadingTask.promise;
    },
  );
  const [pageNum, setPageNum] = createSignal(1);

  createEffect(() => {
    doc(); // reset to page 1 whenever a new document loads
    setPageNum(1);
  });

  onCleanup(() => {
    void loadingTask?.destroy();
  });

  let containerEl: HTMLDivElement | undefined;
  let canvasEl: HTMLCanvasElement | undefined;
  let currentRenderTask: RenderTask | undefined;
  const [renderError, setRenderError] = createSignal<string | null>(null);

  const renderPage = async (): Promise<void> => {
    const d = doc();
    if (!d || !containerEl || !canvasEl) return;
    setRenderError(null);
    try {
      const page = await d.getPage(pageNum());
      const dpr = window.devicePixelRatio || 1;
      const containerWidth = containerEl.clientWidth || 1;
      const unscaled = page.getViewport({ scale: 1 });
      const fitScale = containerWidth / unscaled.width;
      const viewport = page.getViewport({ scale: fitScale * dpr });

      const canvas = canvasEl;
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      canvas.style.width = `${containerWidth}px`;
      canvas.style.height = `${viewport.height / dpr}px`;

      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      currentRenderTask?.cancel();
      const task = page.render({ canvas, canvasContext: ctx, viewport });
      currentRenderTask = task;
      await task.promise;
    } catch (err) {
      // A page render cancelled by a fast page flip isn't a real failure.
      if (err instanceof Error && err.name === "RenderingCancelledException") {
        return;
      }
      setRenderError("Could not render this page.");
    }
  };

  createEffect(() => {
    pageNum();
    doc();
    void renderPage();
  });

  let resizeObserver: ResizeObserver | undefined;
  const attachContainer = (el: HTMLDivElement): void => {
    containerEl = el;
    resizeObserver = new ResizeObserver(() => void renderPage());
    resizeObserver.observe(el);
  };

  onCleanup(() => {
    currentRenderTask?.cancel();
    resizeObserver?.disconnect();
  });

  return (
    <Show
      when={!doc.error}
      fallback={<ErrorState message="Could not load this PDF." />}
    >
      <Show when={settled(doc)} fallback={<LoadingText label="Loading view" />}>
        {(d) => (
          <div class="flex h-full min-h-0 flex-col">
            <div class="flex shrink-0 items-center justify-center gap-3 px-2 py-1.5">
              <Button
                variant="ghost"
                size="sm"
                disabled={pageNum() <= 1}
                onClick={() => setPageNum((n) => Math.max(1, n - 1))}
              >
                Prev
              </Button>
              <Text variant="micro" tone="dim" class="tabular-nums">
                PAGE {pageNum()}/{d().numPages}
              </Text>
              <Button
                variant="ghost"
                size="sm"
                disabled={pageNum() >= d().numPages}
                onClick={() => setPageNum((n) => Math.min(d().numPages, n + 1))}
              >
                Next
              </Button>
            </div>
            <div ref={attachContainer} class="h-full min-h-0 overflow-auto p-2">
              <Show when={renderError()}>
                <ErrorState message={renderError()!} />
              </Show>
              <canvas ref={canvasEl} class="block" />
            </div>
          </div>
        )}
      </Show>
    </Show>
  );
}
