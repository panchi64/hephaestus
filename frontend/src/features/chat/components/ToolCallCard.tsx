import { For, Show, createMemo, type JSX } from "solid-js";
import {
  CodeBlock,
  Collapse,
  Icon,
  REVEAL_BASE,
  StatusFlag,
  Text,
  copyToClipboard,
  cx,
} from "~/ui";
import { compactCount, num } from "~/lib/format";
import { isLiveStatus, type ToolInvocation } from "../model";
import { preparingPhrase, toolEntry } from "../toolPresentation";
import { Branch } from "./Branch";
import { CommandBoundary, FenceNote } from "./CommandBoundary";
import { EnvelopeRow } from "./EnvelopeRow";
import { ProcessRow, Sep, createAdoptedOpen } from "./ProcessRow";
import { ToolScreenshots } from "./ToolScreenshots";

/** The family glyph carries the call's state as well as its kind, so a column of
 *  rows reads at a glance by shape and tone before a word is parsed.
 *
 *  This is now one of only two things that report a running call — the other is
 *  the lit trunk beside it. A single call used to say "running" five ways at once:
 *  the turn's tempo line, the rail's light (the trunk's now), this glyph, a RUNNING
 *  flag at the far right, and a throbber in the middle of the row. Five dialects of one fact is
 *  what made a turn read as a console dump, and it spent the accent budget (§5)
 *  on the most ambient state there is. */
const iconTone: Record<ToolInvocation["status"], string> = {
  preparing: "text-info",
  running: "text-info",
  ok: "text-dim",
  error: "text-alert",
};

/** The elapsed figure is rendered whether or not it has landed, so the copy button
 *  beside it doesn't jump sideways the moment a call returns.
 *
 *  Six characters, not five: `num(s, 2)` plus the `S` suffix is `0.38S` for a fast
 *  call but `12.34S` for anything over ten seconds, and a shell command or a
 *  web fetch is routinely over ten seconds. Reserving five put the shift back
 *  exactly where the reservation was supposed to remove it. (A call over 100s
 *  still gains a character; at that point the operator has not had a pointer
 *  resting on the row for two minutes.) */
const ELAPSED_WIDTH = "min-w-[6ch]";

/** Inline record of a single tool invocation inside an assistant message.
 *
 *  The collapsed row is `glyph · Label · what it was about · what came back` —
 *  the namespaced registry name (`files_read_file`) and the full argument dump
 *  move into the expanded body, where an operator who wants them is already
 *  looking. A turn is a dozen of these rows, and the row has to be scannable
 *  without being read.
 *
 *  **Only failure gets a word.** `StatusFlag` renders for `error` and nothing
 *  else. §10.5 gives warn/alert the accent on the label precisely because those
 *  are the two states that must interrupt; OK and Running are ambient, already
 *  said by the glyph and the trunk, and a row that announces its own success on
 *  every line is a row that has stopped meaning anything. Colour is never the
 *  sole carrier of the failure either (§12) — the word "Failed" is there beside
 *  the alert-toned glyph.
 *
 *  `open` makes expand/collapse controlled (expand-all/collapse-all); when
 *  undefined the card keeps its default behavior (auto-open on error).
 *
 *  **A script's calls are this same row, nested.** A `run_code` card lists the calls
 *  its script made under its own row, each with the anatomy every call has — only the
 *  place differs: `variant="nested"` hangs them off the script's own socket, a trunk of
 *  their own inside the log's, so the line says whose call each is. They list outside
 *  the disclosure, like a screenshot, because what the script did is the point of the
 *  card; the script itself is behind it, where the argument dump is for other tools. */
export function ToolCallCard(props: {
  tool: ToolInvocation;
  open?: boolean;
  variant?: "card" | "nested";
}): JSX.Element {
  const nested = (): boolean => props.variant === "nested";
  // Auto-expand error cards so the reason is immediately visible.
  const { open, toggle } = createAdoptedOpen(
    props,
    props.tool.status === "error",
  );
  const shown = createMemo(() => toolEntry(props.tool.name));
  const preparing = (): boolean => props.tool.status === "preparing";
  const argsSize = (): string | undefined => {
    const n = props.tool.argsChars;
    return !n ? undefined : `${compactCount(n).toLowerCase()} chars`;
  };
  // What the row is *about*, in preference order: the model's own reason for the
  // call, the salient argument, then the full summary — never nothing.
  //
  // The narration leads because it answers a different question from the other two.
  // `Read agent.py` says what happened; "Checking how the run loop handles a cancel"
  // says what it was for, and reading a turn back is almost always the second
  // question. The argument is a keystroke away in the open card either way.
  const detail = () =>
    props.tool.narration ?? props.tool.detail ?? props.tool.args;
  /* A sentence is the row's meaning rather than its metadata, so it takes the
     ordinary text tone where an argument fragment stays dim. Same slot, same size,
     same voice — the column still parses as one sequence, which is the whole reason
     every row here shares `ProcessRow`'s anatomy. */
  const detailTone = (): "dim" | "default" =>
    props.tool.narration ? "default" : "dim";
  // Copy the most useful payload available: result, else error, else the args.
  const copyTool = (e: MouseEvent): void => {
    e.stopPropagation();
    copyToClipboard(
      props.tool.result ??
        props.tool.error ??
        props.tool.script ??
        props.tool.args,
      "Tool result",
    );
  };
  return (
    <div
      /* Two group names, because a nested row sits inside its script's card: under
         one name, hovering anywhere on the script would reveal every nested row's
         copy button at once. */
      class={nested() ? "group/nested" : "group/tool"}
    >
      <ProcessRow
        open={open()}
        onToggle={toggle}
        foldable={preparing() ? false : undefined}
        icon={shown().icon}
        iconClass={iconTone[props.tool.status]}
        frame={shown().frame}
        label={
          preparing() ? `${preparingPhrase(props.tool.name)}…` : shown().label
        }
        title={props.tool.name}
        class="hover:bg-raised"
        trailing={
          <>
            {/* Rendered unconditionally at a fixed width, holding a hard space
                until the figure exists. A `Show` here meant the whole cluster
                grew by five characters the instant a call returned, nudging the
                copy button sideways under a pointer already on it. */}
            <Text
              variant="micro"
              tone="dim"
              class={`${ELAPSED_WIDTH} text-right tabular-nums`}
            >
              {props.tool.elapsedMs === undefined
                ? " "
                : `${num(props.tool.elapsedMs / 1000, 2)}S`}
            </Text>
            <Show when={props.tool.status === "error"}>
              <StatusFlag status="alert">
                {props.tool.platform ? "Platform error" : "Failed"}
              </StatusFlag>
            </Show>
            {/* Running has no visible word by design — the trunk's light and the
                glyph's tone carry it, and that is the whole point of thinning the
                chorus. But both of those are COLOUR, and §12 does not allow an
                accent to be the sole carrier of a state. With parallel calls the
                turn's tempo line only says "Running 3 tools", so without this a
                screen reader cannot tell which of six identical-reading rows is
                still out. Costs nothing visually and restores the fact. */}
            <Show when={isLiveStatus(props.tool.status)}>
              <span class="sr-only">
                {preparing() ? "Preparing" : "Running"}
              </span>
            </Show>
            <button
              type="button"
              aria-label="Copy tool result"
              onClick={copyTool}
              class={cx(
                REVEAL_BASE,
                "text-dim hover:text-bright",
                nested()
                  ? "group-hover/nested:opacity-100"
                  : "group-hover/tool:opacity-100",
              )}
            >
              <Icon name="copy" size={12} />
            </button>
          </>
        }
      >
        <Show when={preparing() && argsSize()}>
          {(size) => (
            <>
              <Sep />
              <Text variant="micro" tone="dim" class="shrink-0 tabular-nums">
                {size()}
              </Text>
            </>
          )}
        </Show>
        <Show
          when={props.tool.status === "running" && props.tool.progress}
          fallback={
            <Show when={!preparing() && detail()}>
              <Sep />
              <Text
                variant="micro"
                tone={detailTone()}
                class="min-w-0 truncate"
              >
                {detail()}
              </Text>
            </Show>
          }
        >
          <Sep />
          {/* A progress note is information — what the wait is actually doing —
              not a restatement of "running", so it survives the cull. */}
          <Text variant="micro" tone="info" class="min-w-0 truncate">
            {props.tool.progress}
          </Text>
        </Show>
        <Show when={props.tool.status === "ok" && props.tool.outcome}>
          <Sep />
          <Text variant="micro" tone="default" class="min-w-0 truncate">
            {props.tool.outcome}
          </Text>
        </Show>
      </ProcessRow>
      {/* The script's calls hang off the script's own socket, a trunk of their own
          inside the log's — the same schematic one level down. Directly under the
          row, before any screenshot: the first branch reaches a fixed 6px up to the
          socket's foot, and a strip between them would cut the trunk off. */}
      <Show when={props.tool.children}>
        {(kids) => (
          <For each={kids()}>
            {(child, i) => (
              <Branch
                variant="nested"
                edge={{ first: i() === 0, last: i() === kids().length - 1 }}
                lit={isLiveStatus(child.status)}
              >
                <ToolCallCard tool={child} open={props.open} variant="nested" />
              </Branch>
            )}
          </For>
        )}
      </Show>
      <Show when={props.tool.images?.length ? props.tool.images : undefined}>
        {(images) => (
          <ToolScreenshots images={images()} toolName={props.tool.name} />
        )}
      </Show>
      <Collapse open={open()}>
        <div class="flex flex-col gap-1 px-2 py-2">
          {/* The registry name and every argument — what the collapsed row trades
              away for scannability, restored the moment the operator asks.
              Height-capped and scrollable: `formatArgs` does not truncate, and a
              `code_execute` call's arguments ARE its script, which would
              otherwise push the result the operator opened the card for off the
              bottom of the screen. */}
          <Text
            as="div"
            variant="micro"
            tone="dim"
            class="max-h-24 overflow-y-auto break-words"
          >
            <span class="text-text">{props.tool.name}</span>
            {props.tool.args ? ` ${props.tool.args}` : ""}
          </Text>
          {/* A script is a program, so it reads as one — lines and a gutter, capped
              like the dump above so the result stays within reach. */}
          <Show when={props.tool.script}>
            {(script) => (
              <CodeBlock
                code={script()}
                lang="python"
                fontStep={-1}
                class="max-h-60 border border-line"
              />
            )}
          </Show>
          {/* A call that ran a command without being a terminal — a backgrounded
              process, which is a handle the agent checks on later rather than output
              the operator watches arrive. It still ran something, and this is the
              only row that says where. */}
          <Show when={props.tool.boundary}>
            {(boundary) => (
              <>
                <CommandBoundary command={boundary()} />
                <FenceNote command={boundary()} />
              </>
            )}
          </Show>
          <Show
            when={props.tool.status === "error" && props.tool.error}
            fallback={
              <Show
                when={props.tool.resultParts}
                fallback={
                  <Show when={props.tool.result}>
                    <Text
                      variant="micro"
                      tone="dim"
                      class="whitespace-pre-wrap break-words"
                    >
                      {props.tool.result}
                    </Text>
                  </Show>
                }
              >
                <For each={props.tool.resultParts}>
                  {(part) => (
                    <Show
                      when={part.kind !== "text"}
                      fallback={
                        <Text
                          variant="micro"
                          tone="dim"
                          class="whitespace-pre-wrap break-words"
                        >
                          {part.text}
                        </Text>
                      }
                    >
                      <EnvelopeRow variant="part" part={part} scope="tool" />
                    </Show>
                  )}
                </For>
              </Show>
            }
          >
            <Text
              variant="micro"
              tone="alert"
              class="whitespace-pre-wrap break-words"
            >
              {props.tool.error}
            </Text>
          </Show>
          <Show
            when={
              props.tool.status === "error" &&
              !props.tool.error &&
              !props.tool.result
            }
          >
            <Text variant="micro" tone="alert">
              Tool failed with no additional detail.
            </Text>
          </Show>
        </div>
      </Collapse>
    </div>
  );
}
