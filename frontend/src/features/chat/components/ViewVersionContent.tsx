import {
  createEffect,
  createMemo,
  createResource,
  Match,
  Show,
  Switch,
  type JSX,
} from "solid-js";
import { api, useAuthedBlobUrl } from "~/lib/api";
import { bytes } from "~/lib/format";
import { Button, CodeBlock, ErrorState, LoadingText, Text } from "~/ui";
import type { ViewPreviewRef } from "../model";
import { detectContentKind, extensionOf } from "../viewport/viewItems";
import { createDownloadSlot, downloadBlob } from "../viewport/downloadRegistry";
import { CsvTable } from "./renderers/CsvTable";
import { fontStepStyle } from "./renderers/fontStep";
import { JsonTree } from "./renderers/JsonTree";
import { MediaPlayer } from "./renderers/MediaPlayer";
import { PdfViewer } from "./renderers/PdfViewer";
import { RawTextViewer } from "./renderers/RawTextViewer";
import { SandboxedFrame } from "./SandboxedFrame";
import { SvgContent } from "./renderers/SvgContent";
import { settled } from "~/lib/resource";

/** Text/code under this size render inline (existing `pre` / `CodeBlock`); at or
 *  over it, the same bytes hand off to `RawTextViewer`'s virtualized rendering. */
const INLINE_TEXT_THRESHOLD = 200_000;

/**
 * Renders a version's static **preview** — the captured file a `show(file=…)` stamped on
 * it — routed by detected content kind (CODE is the version's workspace tree, shown by
 * `ViewSnapshotCode`, so this component only ever renders the preview). HTML and image
 * bytes are auth-gated and resolve through the shared blob-URL hook (an `<img>` / iframe
 * `src` can't carry a bearer); every other kind fetches the raw bytes once and hands them
 * to the matching `chat/components/renderers/*` component. Whenever an artifact is on
 * stage, the panel-level download button is armed with its already-fetched bytes. HTML
 * renders in an opaque-origin sandboxed iframe — no `allow-same-origin`, so model-generated
 * markup can't act as the operator.
 */
export function ViewVersionContent(props: {
  preview: ViewPreviewRef;
  title: string;
  /** Manual reload nonce — bumping it reloads the framed HTML preview in place. */
  reloadKey: number;
  /** Zoom step passed through to the renderers that support it. Default 0. */
  fontStep?: number;
  /** Soft-wrap passed through to the renderers that support it. Default false. */
  softWrap?: boolean;
}): JSX.Element {
  const contentPath = (): string =>
    `/views/${props.preview.artifactId}/content`;
  const scrollKey = (): string => `view:${props.preview.artifactId}`;

  const kind = createMemo(() =>
    detectContentKind(props.title, props.preview.kind),
  );

  // image + HTML render through the blob-URL hook (their src can't carry a bearer) —
  // unchanged from the prior implementation.
  const isUrlKind = (): boolean => kind() === "image" || kind() === "html";
  const objectUrl = useAuthedBlobUrl(() =>
    isUrlKind() ? contentPath() : undefined,
  );

  // Every other kind — plus the panel-level download button, for every kind — wants
  // the raw bytes, fetched once per artifact independent of the url-kind path above.
  const [blob, { refetch: refetchBlob }] = createResource(
    () => props.preview.artifactId,
    () => api.getBlob(contentPath()),
  );

  // Whenever this component has an artifact's bytes in hand, arm the panel-level
  // download button with them — a relay of what's already been fetched, not a
  // decision the frontend makes on its own.
  const armDownload = createDownloadSlot();
  createEffect(() => {
    const b = blob();
    if (!b) return;
    armDownload({ name: props.title, getBlob: async () => b });
  });

  const isTextLike = (): boolean => kind() === "text" || kind() === "code";
  // Assume inline while the size isn't known yet, so the small/common case never
  // flashes the RawTextViewer arm first; a genuinely large file flips this once
  // `blob()` resolves with its real size.
  const inline = (): boolean =>
    (settled(blob)?.size ?? 0) < INLINE_TEXT_THRESHOLD;

  const [text] = createResource(
    () => (isTextLike() && inline() ? settled(blob) : undefined),
    (b) => b.text(),
  );

  const codeLang = (): string | undefined =>
    extensionOf(props.title) ?? undefined;

  const urlArm = (render: (url: string) => JSX.Element): JSX.Element => (
    <Switch fallback={<LoadingText label="Loading view…" />}>
      <Match when={objectUrl.error}>
        <ErrorState message="Could not load this version." />
      </Match>
      <Match when={settled(objectUrl)}>{(url) => render(url())}</Match>
    </Switch>
  );

  const blobArm = (render: (b: Blob) => JSX.Element): JSX.Element => (
    <Switch fallback={<LoadingText label="Loading view…" />}>
      <Match when={blob.error}>
        <ErrorState
          message="Could not load this version."
          onRetry={() => void refetchBlob()}
        />
      </Match>
      <Match when={settled(blob)}>{(b) => render(b())}</Match>
    </Switch>
  );

  return (
    <Switch>
      <Match when={kind() === "image"}>
        {urlArm((url) => (
          <img
            src={url}
            alt={props.title}
            class="h-full w-full object-contain"
          />
        ))}
      </Match>
      <Match when={kind() === "html"}>
        {urlArm((url) => (
          <SandboxedFrame
            src={url}
            title={props.title}
            reloadKey={props.reloadKey}
            fontStep={props.fontStep}
          />
        ))}
      </Match>
      <Match when={kind() === "svg"}>
        {blobArm((b) => (
          <SvgContent
            data={b}
            name={props.title}
            fontStep={props.fontStep}
            softWrap={props.softWrap}
            scrollKey={scrollKey()}
          />
        ))}
      </Match>
      <Match when={kind() === "csv"}>
        {blobArm((b) => (
          <CsvTable
            data={b}
            name={props.title}
            fontStep={props.fontStep}
            softWrap={props.softWrap}
            scrollKey={scrollKey()}
          />
        ))}
      </Match>
      <Match when={kind() === "json"}>
        {blobArm((b) => (
          <JsonTree
            data={b}
            name={props.title}
            fontStep={props.fontStep}
            softWrap={props.softWrap}
            scrollKey={scrollKey()}
          />
        ))}
      </Match>
      <Match when={kind() === "pdf"}>
        {blobArm((b) => (
          <PdfViewer
            data={b}
            name={props.title}
            fontStep={props.fontStep}
            softWrap={props.softWrap}
            scrollKey={scrollKey()}
          />
        ))}
      </Match>
      <Match when={kind() === "audio" || kind() === "video"}>
        {blobArm((b) => (
          <MediaPlayer
            data={b}
            name={props.title}
            fontStep={props.fontStep}
            softWrap={props.softWrap}
            scrollKey={scrollKey()}
          />
        ))}
      </Match>
      <Match when={isTextLike() && inline()}>
        <Switch fallback={<LoadingText label="Loading view…" />}>
          <Match when={text.error}>
            <ErrorState message="Could not load this version." />
          </Match>
          <Match when={settled(text) !== undefined}>
            <Show
              when={kind() === "code"}
              fallback={
                <Text
                  as="pre"
                  variant="code"
                  class="block h-full overflow-auto whitespace-pre-wrap break-words p-3"
                  style={fontStepStyle(props.fontStep)}
                >
                  {settled(text)}
                </Text>
              }
            >
              <CodeBlock
                code={settled(text) ?? ""}
                lang={codeLang()}
                fontStep={props.fontStep}
                softWrap={props.softWrap}
              />
            </Show>
          </Match>
        </Switch>
      </Match>
      <Match when={isTextLike() && !inline()}>
        {blobArm((b) => (
          <RawTextViewer
            data={b}
            name={props.title}
            fontStep={props.fontStep}
            softWrap={props.softWrap}
            scrollKey={scrollKey()}
          />
        ))}
      </Match>
      <Match when={kind() === "other"}>
        {blobArm((b) => (
          <div class="flex h-full flex-col items-center justify-center gap-2 p-4">
            <Text variant="micro" tone="dim">
              {props.title}
            </Text>
            <Text variant="micro" tone="dim">
              {bytes(b.size)} · {kind().toUpperCase()}
            </Text>
            <Button
              variant="ghost"
              size="sm"
              leading="download"
              onClick={() => downloadBlob(props.title, b)}
            >
              Download
            </Button>
          </div>
        ))}
      </Match>
    </Switch>
  );
}
