import { For, Show, type JSX } from "solid-js";
import { EmptyState, StatusFlag, Text, type Status } from "~/ui";
import { duration } from "~/lib/format";
import type { HostCommand, HostCommandPhase } from "../model";
import { CommandBoundary, FenceNote } from "./CommandBoundary";
import { createAdoptedOpen } from "./ProcessRow";
import { SurfaceBody, SurfaceCard } from "./surfaceChrome";

/** One stream of output, whichever it is. Capped so a chatty build's log scrolls inside
 *  its own row rather than pushing every command after it off the pane; wrapped hard
 *  because a long unbroken token (a path, a hash) must not widen the pane. */
const OUTPUT = "max-h-72 overflow-y-auto whitespace-pre-wrap break-all";

/** The row's state as one word, in the same vocabulary the inline terminal uses — the
 *  panel and the transcript are two views of one command and must not name it
 *  differently. */
const phaseFlag: Record<HostCommandPhase, { status: Status; label: string }> = {
  pending: { status: "warn", label: "Waiting" },
  running: { status: "info", label: "Running" },
  ok: { status: "nominal", label: "OK" },
  error: { status: "alert", label: "Failed" },
  denied: { status: "alert", label: "Denied" },
  stale: { status: "idle", label: "Decided elsewhere" },
};

const iconTone: Record<HostCommandPhase, string> = {
  pending: "text-warn",
  running: "text-info",
  ok: "text-dim",
  error: "text-alert",
  denied: "text-alert",
  stale: "text-dim",
};

/**
 * Everything a thread has run, as one readout.
 *
 * The transcript already shows each command where it happened, and that is the right
 * place to *watch* one — but it is the wrong place to answer the question this panel
 * exists for: **what has this thread done to my machine, and did any of it fail?** In
 * the transcript a command is separated from the one before it by a turn of reasoning
 * and half a screen of prose, so a failure eight commands back is found by scrolling
 * and remembering. Here the same commands are one column, and the failures are the rows
 * that are already open.
 *
 * **Failed commands rest open; everything else rests shut.** A successful command's
 * output is a record — kept, and read later if at all — while a failed one's output is
 * the whole reason the operator came to this panel, and putting it one click away is
 * putting it behind a click nobody knows to make. The same rule the inline tool card
 * has always followed, applied to a surface whose whole content is commands.
 *
 * Deliberately **read-only**: there is nothing to press here. Approving a command is
 * answered in the dock that takes the composer's slot, where it cannot scroll out of
 * reach; a panel offering a second APPROVE would be a second place to answer one
 * question, and the two would disagree the moment one of them was stale.
 */
export function CommandsSurface(props: {
  commands: () => HostCommand[];
}): JSX.Element {
  return (
    <SurfaceBody gap={1}>
      <Show
        when={props.commands().length}
        fallback={
          <EmptyState
            icon="terminal"
            message="No commands yet"
            hint="What the agent runs in this thread lands here."
          />
        }
      >
        <For each={props.commands()}>
          {(command) => <CommandRow command={command} />}
        </For>
      </Show>
    </SurfaceBody>
  );
}

/** One command: the line it ran, how it ended, and — opened — what it printed. */
function CommandRow(props: { command: HostCommand }): JSX.Element {
  const c = () => props.command;
  const failed = () => c().phase === "error" || c().phase === "denied";
  // **A failure opens the row, whenever it becomes one.** Passing the resting state as
  // the initial value alone would only cover a command that had already failed when the
  // panel mounted — a command the operator is watching run would fail in front of them
  // and stay shut. `undefined` is the adopter's "nobody is driving this", so this reads
  // as: on failure, open; otherwise leave it to whoever last clicked it.
  const { open, toggle } = createAdoptedOpen({
    get open() {
      return failed() ? true : undefined;
    },
  });
  const flag = () =>
    phaseFlag[c().phase] ?? { status: "idle" as Status, label: c().phase };
  // While it runs the two streams arrive concatenated, so the body shows that one
  // text; once it settles the result has them apart and they render as themselves.
  const settled = () => c().phase !== "running" && c().phase !== "pending";
  const hasBody = () =>
    Boolean(c().stdout) ||
    Boolean(c().stderr) ||
    Boolean(c().streamed) ||
    c().error != null ||
    c().fenceNote != null ||
    c().reach != null;

  return (
    <SurfaceCard
      open={open()}
      onToggle={toggle}
      icon="terminal"
      iconClass={iconTone[c().phase] ?? "text-dim"}
      label="Command"
      title={c().name}
      /* The command line IS the row — no label reads it better than itself. */
      detail={c().command}
      gap={1}
      trailing={
        <>
          {/* An unfenced command is the exception worth seeing without opening the
              row — the fence is what the declaration is worth, and a thread where it
              did not apply is a thread the operator should know about at rest. The
              rest of the boundary reads inside, where there is room for the reason. */}
          <Show when={c().fenced === false}>
            <StatusFlag status="warn">Unfenced</StatusFlag>
          </Show>
          <Show when={c().elapsedMs !== undefined}>
            <Text variant="micro" tone="dim" class="tabular-nums">
              {duration(c().elapsedMs!)}
            </Text>
          </Show>
          <StatusFlag status={flag().status} dot>
            {flag().label}
          </StatusFlag>
        </>
      }
    >
      <Show
        when={hasBody()}
        fallback={
          <Text variant="micro" tone="dim">
            Nothing to show yet.
          </Text>
        }
      >
        <CommandBoundary command={c()} />
        <Show when={settled()} fallback={<StreamedText command={c()} />}>
          <Show when={c().stdout}>
            <Text as="div" variant="micro" tone="default" class={OUTPUT}>
              {c().stdout}
            </Text>
          </Show>
          <Show when={c().stderr}>
            <Text as="div" variant="micro" tone="alert" class={OUTPUT}>
              {c().stderr}
            </Text>
          </Show>
        </Show>
        <Show when={c().error}>
          <Text variant="micro" tone="warn" class="break-words">
            {c().error}
          </Text>
        </Show>
        <FenceNote command={c()} />
        <Show when={c().timedOut || c().exitCode != null}>
          <Text variant="micro" tone={c().exitCode === 0 ? "dim" : "alert"}>
            {c().timedOut ? "TIMED OUT" : `EXIT ${c().exitCode}`}
          </Text>
        </Show>
      </Show>
    </SurfaceCard>
  );
}

/** Output arriving while the command still runs — one stream, because that is how many
 *  the wire can tell apart mid-flight. */
function StreamedText(props: { command: HostCommand }): JSX.Element {
  return (
    <Show when={props.command.streamed}>
      <Text as="div" variant="micro" tone="default" class={OUTPUT}>
        {props.command.streamed}
      </Text>
    </Show>
  );
}
