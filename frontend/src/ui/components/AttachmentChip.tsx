import { Show, type JSX } from "solid-js";
import { cx } from "../cx";
import { Icon } from "../primitives/Icon";
import { Text, type TextTone } from "../primitives/Text";
import { CHIP_BASE } from "./Chip";
import { Tooltip } from "./Tooltip";

/** Lifecycle of one attached file as the composer sees it. `uploading` and
 *  `extracting` are in-flight; `ready` can be sent; `error` failed to ingest. */
export type AttachmentStatus = "uploading" | "extracting" | "ready" | "error";

/** Presentational shape of a file attached to a message. The feature layer owns
 *  the real upload/poll; the chip only renders this. */
export interface ComposerAttachment {
  id: string;
  name: string;
  status: AttachmentStatus;
  /** Excluded from the knowledge base / retrieval corpus when true. */
  kbExcluded: boolean;
  /** Why an `error` chip failed, in the backend's words, when it gave any. */
  error?: string;
}

export interface AttachmentChipProps {
  /** Filename to show. */
  name: string;
  /** Drives the inline status hint + tone. Omit for a plain (sent) chip. */
  status?: AttachmentStatus;
  /** When set, the chip is a link to the file (read-only / sent message use). */
  href?: string;
  /** KB membership — renders the in-KB / excluded marker when defined. */
  kbExcluded?: boolean;
  /** Editable use: toggle KB membership. Omit to show KB state read-only. */
  onToggleKbExcluded?: () => void;
  /** Editable use: remove this attachment before sending. */
  onRemove?: () => void;
  /** Why the file failed, shown on hover over an `error` chip's status. Falls back
   *  to "Upload failed" — a bare "Failed" with nothing behind it tells the operator
   *  that something is wrong and nothing about whether retrying could help. */
  error?: string;
  /** Editable use: re-run a failed upload. Rendered only on an `error` chip. */
  onRetry?: () => void;
  /** `pill` (default): the rounded micro pill a sent message carries. `console`: the
   *  composer's square mono chip — a hairline box, the file's type as a dim uppercase
   *  tag where the glyph was, then the name — so it reads as one more machine cell of
   *  the ruled unit it sits in. */
  variant?: "pill" | "console";
  class?: string;
}

/** The file's type, as the console chip's tag: its extension, or `file`. */
const typeTag = (name: string): string => {
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(dot + 1) : "file";
};

const CONSOLE_BASE =
  "inline-flex max-w-full items-center gap-2 border border-line px-2 py-1 font-mono text-meta text-text";

const STATUS_HINT: Record<AttachmentStatus, string> = {
  uploading: "Uploading…",
  extracting: "Extracting…",
  ready: "",
  error: "Failed",
};

// Accents carry meaning only: in-flight is live `info`, failure is `alert`. A
// `ready` chip is at rest, so it shows no hint and no accent — the resting
// interface stays monochrome (the chip's neutral `dim` border is enough).
const STATUS_TONE: Record<AttachmentStatus, TextTone> = {
  uploading: "info",
  extracting: "info",
  ready: "dim",
  error: "alert",
};

/**
 * One attached file, rendered as a bordered chip: a file glyph, the name, an
 * optional status hint, a KB-membership marker, and (when editable) KB-toggle
 * and remove controls. The same chip serves the composer (editable) and a sent
 * message (read-only link) — a variant, never a fork. Built on the same bordered
 * micro-pill base as `Chip`, with the richer internal structure those uses need.
 */
export function AttachmentChip(props: AttachmentChipProps): JSX.Element {
  const failed = () => props.status === "error";
  const hint = () => (
    <Text variant="micro" tone={STATUS_TONE[props.status!]} class="shrink-0">
      {STATUS_HINT[props.status!]}
    </Text>
  );
  const square = () => props.variant === "console";
  const body = (
    <>
      <Show
        when={square()}
        fallback={<Icon name="file" size={12} class="shrink-0" />}
      >
        <span class="shrink-0 uppercase tracking-label text-dim">
          {typeTag(props.name)}
        </span>
      </Show>
      <span class="truncate max-w-40">{props.name}</span>
      <Show when={props.status && STATUS_HINT[props.status]}>
        {/* The reason rides on the status word rather than on the whole chip, so
            hovering the name or the controls beside it doesn't throw a tip over
            them. */}
        <Show when={failed()} fallback={hint()}>
          <Tooltip label={props.error ?? "Upload failed"} side="top">
            {hint()}
          </Tooltip>
        </Show>
      </Show>
    </>
  );

  return (
    <span class={cx(square() ? CONSOLE_BASE : CHIP_BASE, props.class)}>
      <Show
        when={props.href}
        fallback={
          <span
            class={cx(
              "inline-flex min-w-0 items-center",
              square() ? "gap-2" : "gap-1",
            )}
          >
            {body}
          </span>
        }
      >
        <a
          href={props.href}
          class="inline-flex items-center gap-1 transition-colors hover:text-bright"
        >
          {body}
        </a>
      </Show>

      {/* KB membership reads as the *exception*: an included file is the resting
          default (neutral `dim`), an excluded one carries a `warn` accent —
          color marks the deviation, never decorates the norm. */}
      <Show when={props.kbExcluded !== undefined}>
        <Show
          when={props.onToggleKbExcluded}
          fallback={
            <span
              class={cx(
                "shrink-0",
                props.kbExcluded ? "text-warn" : "text-dim",
              )}
              title={
                props.kbExcluded
                  ? "Excluded from knowledge base"
                  : "In knowledge base"
              }
            >
              <Icon name="database" size={12} />
            </span>
          }
        >
          <button
            type="button"
            onClick={() => props.onToggleKbExcluded!()}
            class={cx(
              "shrink-0 transition-colors hover:text-bright",
              props.kbExcluded ? "text-warn" : "text-dim",
            )}
            aria-label={
              props.kbExcluded
                ? "Include in knowledge base"
                : "Exclude from knowledge base"
            }
            title={
              props.kbExcluded
                ? "Excluded from knowledge base — click to include"
                : "In knowledge base — click to exclude"
            }
          >
            <Icon name="database" size={12} />
          </button>
        </Show>
      </Show>

      <Show when={failed() && props.onRetry}>
        <button
          type="button"
          onClick={() => props.onRetry!()}
          class="shrink-0 text-dim transition-colors hover:text-bright"
          aria-label={`Retry ${props.name}`}
          title="Retry"
        >
          <Icon name="refresh" size={12} />
        </button>
      </Show>

      <Show when={props.onRemove}>
        <button
          type="button"
          onClick={() => props.onRemove!()}
          class="shrink-0 text-dim transition-colors hover:text-alert"
          aria-label={`Remove ${props.name}`}
        >
          <Show when={square()} fallback={<Icon name="close" size={12} />}>
            <span aria-hidden="true">×</span>
          </Show>
        </button>
      </Show>
    </span>
  );
}
