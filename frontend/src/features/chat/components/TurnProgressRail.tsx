import { Show, type JSX } from "solid-js";
import { Collapse, Frames, Row, Text } from "~/ui";
import type { AssistantBlock } from "../model";
import { runningTools } from "../blocks";
import { toolPresentation } from "../toolPresentation";

/** What the agent is doing *right now* — live tool calls first, then the trailing
 *  block, which is the one receiving deltas when no tool is out. Calls go out in
 *  parallel, so the tail is an arbitrary member of the batch: reading it alone would
 *  drop the others and fall back to "Working" the moment that one returned. */
function activeLabel(blocks: AssistantBlock[] | undefined): string {
  const running = runningTools(blocks);
  // Sentence case, like every other branch below: this line renders in the sans
  // `label` variant — the interface's own voice, which the design system keeps in
  // sentence case — and the tool label spliced in is sentence case too, so
  // shouting the prefix would put two registers inside one string.
  if (running.length > 1) return `Running ${running.length} tools`;
  // The card's own label, not the namespaced registry name: this line and the
  // card beneath it are describing the same call, so they should say the same word.
  if (running.length === 1)
    return `Running ${toolPresentation(running[0].name).label}`;
  const last = blocks?.[blocks.length - 1];
  // No blocks yet = the run was created but nothing has streamed back: the backend
  // is still preparing (context assembly, model spin-up). Say so rather than
  // "Working", which implies the agent is already mid-task.
  if (!last) return "Starting";
  switch (last.kind) {
    case "thinking":
      return "Thinking";
    case "text":
      return "Writing";
    // `runningTools` above claims every live call, so a tool block reaching here has
    // already returned.
    case "tool":
      return "Working";
    // Injections all land before the model has said anything, so a context block at the
    // tail means the turn is still being assembled — the phase the no-blocks branch above
    // could only guess at, now that it has something to point to.
    case "context":
      return "Assembling context";
    case "host_command":
      return last.command.phase === "pending"
        ? "Awaiting approval"
        : last.command.phase === "running"
          ? "Running on host"
          : "Working";
    case "approval":
      return "Awaiting approval";
    // Both parks say what they are waiting for rather than sharing a word: "awaiting
    // approval" on a turn that asked which database to use would send the operator
    // looking for a decision the dock is not offering them.
    case "question":
      return "Awaiting your answer";
    case "view_version":
      return "Updating view";
    case "view_live":
      return "Starting live view";
  }
}

/** The turn's tempo line: a hard-stepped throbber and a label for the live phase
 *  ("Thinking", "Running web search", "Writing"), for as long as the turn runs.
 *
 *  **It is the turn's only *worded* status, and it exists only while the turn is
 *  live.** It used to also print a settled "N Tools · M Thinks" summary, which
 *  sat directly above either the rows it was counting or a work log whose own
 *  header now names those tools by name — a third telling of a fact the operator
 *  could already read twice. The shape summary in the collapsed work log
 *  (`WorkLogHeader`) is the settled answer; this line is the live one.
 *
 *  Everything else that reports a running call does so without words — the rail's
 *  light and the tool row's glyph tone. That division is the point: one sentence,
 *  and the rest in light. */
export function TurnProgressRail(props: {
  blocks: AssistantBlock[] | undefined;
  streaming?: boolean;
  /** True until the run's first event arrives — waiting behind the backend's
   *  concurrency limit, not yet actually executing. Rendered as an explicit
   *  "Queued" state instead of the throbber, which would otherwise look
   *  identical to a model that's just slow to produce its first token. */
  queued?: boolean;
}): JSX.Element {
  /* The line has something to say only while the turn runs, so at the end it goes
     from "Thinking" to nothing at all — and everything below it would jump up by
     the height of the line plus its stack gap the instant the run ends.
     `Collapse` turns that into the region closing, which is a movement the eye
     can follow rather than a jump it has to recover from. */
  return (
    <Collapse open={Boolean(props.streaming)}>
      <Row gap={2} align="center" aria-live="polite">
        <Show
          when={!props.queued}
          fallback={
            <Text variant="label" tone="dim">
              Queued
            </Text>
          }
        >
          <Frames class="text-info" />
          <Text variant="label" tone="info">
            {activeLabel(props.blocks)}
          </Text>
        </Show>
      </Row>
    </Collapse>
  );
}
