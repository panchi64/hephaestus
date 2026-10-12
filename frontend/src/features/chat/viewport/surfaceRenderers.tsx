/**
 * The one place a surface id becomes a component.
 *
 * `surfaces.ts` says what exists and in what order; this says what each entry renders,
 * and nothing else knows both — so adding a surface is one row in each file. The map is
 * keyed by `SurfaceId`, which is itself derived from the registry array, so a surface
 * declared without a renderer is a **compile error** rather than a pane that mounts
 * blank. (The settings dialog's equivalent map settles for a dev-time warning, because
 * importing it constructs sixteen features' resources; these are chat components already
 * in the bundle, so the type error is free and strictly better.)
 *
 * Every value is a **thunk**, not an element. Solid evaluates JSX eagerly, so a map of
 * elements would construct every surface — and every resource behind it — the moment this
 * module is read, rather than when one is actually on screen.
 *
 * A renderer takes the context and reaches into it for what its own surface needs. That
 * is deliberately not a per-surface prop bag: the context carries what is true of *any*
 * surface, and a surface's own data is its business to fetch from the facade.
 */

import { createMemo, Show, type JSX } from "solid-js";
import { StatusFlag, Text } from "~/ui";
import { plural } from "~/lib/format";
import { SubagentsSurface } from "../components/SubagentsSurface";
import { CommandsSurface } from "../components/CommandsSurface";
import { SourcesSurface } from "../components/SourcesSurface";
import { DiffSurface } from "../components/DiffSurface";
import { FilesSurface } from "../components/FilesSurface";
import { PlanSurface, planStatusOf } from "../components/PlanSurface";
import { TasksSurface } from "../components/TasksSurface";
import { ViewportPanel } from "../components/ViewportPanel";
import { taskSummary } from "../components/TaskRows";
import { isLive } from "../data";
import { flattenGroups, groupTasks } from "../taskGroups";
import { failedCount } from "./commandItems";
import type { ChatViewport } from "../useChatViewport";
import { SURFACE_BY_ID, type SurfaceId } from "./surfaces";

/** What every surface is handed. Generic on purpose — see the module note. */
export interface SurfaceContext {
  viewport: ChatViewport;
  /** Dismiss the panel from this surface's close control. The mount supplies it,
   *  because leaving the aside and leaving the full-screen sheet are different acts. */
  onClose: () => void;
  onToggleFullscreen: () => void;
}

export const SURFACE_RENDERERS: Record<
  SurfaceId,
  (ctx: SurfaceContext) => JSX.Element
> = {
  tasks: (ctx) => {
    const spec = SURFACE_BY_ID.tasks;
    return (
      <TasksSurface
        items={ctx.viewport.tasks}
        subagents={ctx.viewport.subagents}
        maxRows={spec.shape === "strip" ? spec.maxRows : 8}
      />
    );
  },
  plan: (ctx) => <PlanSurface plan={ctx.viewport.plan} />,
  agents: (ctx) => (
    <SubagentsSurface
      subagents={ctx.viewport.subagents}
      onSettled={ctx.viewport.refetchSubagents}
    />
  ),
  commands: (ctx) => <CommandsSurface commands={ctx.viewport.commands} />,
  diff: (ctx) => (
    <DiffSurface
      branch={ctx.viewport.branch}
      onChanged={ctx.viewport.refetchBranch}
      fontStep={ctx.viewport.state().fontStep}
      softWrap={ctx.viewport.state().softWrap}
    />
  ),
  sources: (ctx) => <SourcesSurface inventory={ctx.viewport.sources} />,
  files: (ctx) => (
    <FilesSurface
      branch={ctx.viewport.branch}
      fontStep={ctx.viewport.state().fontStep}
      softWrap={ctx.viewport.state().softWrap}
    />
  ),
  view: (ctx) => (
    <ViewportPanel
      items={ctx.viewport.items()}
      selectedKey={ctx.viewport.viewState().pinnedKey}
      onSelect={ctx.viewport.selectView}
      activeTab={ctx.viewport.viewState().activeTab}
      onSelectTab={ctx.viewport.requestTab}
      fontStep={ctx.viewport.state().fontStep}
      onFontStep={(step) => ctx.viewport.patch({ fontStep: step })}
      softWrap={ctx.viewport.state().softWrap}
      onToggleWrap={() =>
        ctx.viewport.patch({ softWrap: !ctx.viewport.state().softWrap })
      }
      fullscreen={ctx.viewport.state().fullscreen}
      onToggleFullscreen={ctx.onToggleFullscreen}
      onKeeper={ctx.viewport.toggleKeeper}
    />
  ),
};

/**
 * What a pane's header says about itself beside its name — a progress figure, a count.
 *
 * A **second map rather than a second field on the renderer**, and the reason is
 * mechanical: the header and the body are drawn in different places (a leaf's frame, a
 * stack's tab strip), so a single renderer returning both would have to be *called* in
 * both — and calling it twice constructs the surface twice, resources and all. Meta is
 * cheap and derived; a body is not.
 *
 * Partial on purpose. A surface with nothing to report says nothing, and having to write
 * `undefined` into this map to add one would be a row that carries no information.
 */
export const SURFACE_META: Partial<
  Record<SurfaceId, (ctx: SurfaceContext) => JSX.Element>
> = {
  tasks: (ctx) => {
    // The same derivation the surface itself used to print, moved rather than
    // rewritten — `groupTasks` is what puts a sub-agent's list beside the thread's,
    // and a count over the raw items would disagree with the rows below it.
    //
    // Memoized because it is not cheap and the template reads it twice: `groupTasks`
    // builds fresh group objects on every call, and the sub-agent poll invalidates it
    // every few seconds.
    const summary = createMemo(() =>
      taskSummary(
        flattenGroups(
          groupTasks(ctx.viewport.tasks(), ctx.viewport.subagents()),
        ),
      ),
    );
    // An empty list has no progress to report, and "0/0" read as one.
    return (
      <Show when={summary().total > 0}>
        <Text variant="micro" tone="dim" class="tabular-nums">
          {`${summary().done}/${summary().total}`}
        </Text>
      </Show>
    );
  },
  plan: (ctx) => {
    // The status is the pane's, like every other surface's figure — the body is the
    // document, and a flag above its title read as the document's first line.
    const status = createMemo(() => {
      const plan = ctx.viewport.plan();
      return plan ? planStatusOf(plan.status) : null;
    });
    return (
      <Show when={status()}>
        {(s) => (
          <StatusFlag status={s().tone} dot>
            {s().label}
          </StatusFlag>
        )}
      </Show>
    );
  },
  agents: (ctx) => {
    // The count decides the wording as well as supplying the number: a thread with
    // nothing in the pane has neither "0 working" nor "0 done" to say, and says nothing.
    // One pass over the list rather than the three the same figures read back as.
    const count = createMemo(() => {
      const all = ctx.viewport.subagents();
      return { total: all.length, working: all.filter(isLive).length };
    });
    return (
      <Show when={count().total > 0}>
        <Text variant="micro" tone="dim">
          {count().working > 0
            ? `${count().working} working`
            : `${count().total} done`}
        </Text>
      </Show>
    );
  },
  sources: (ctx) => {
    // **Two figures, because either alone misleads.** A source count says how much was
    // read; an origin count says how much of it is independent, and five pages off one
    // site is a different piece of evidence from five sites agreeing. The header has
    // room for both only because both are short.
    const count = createMemo(() => {
      const inv = ctx.viewport.sources();
      return { total: inv.items.length, origins: inv.originCount };
    });
    return (
      <Show when={count().total > 0}>
        <Text variant="micro" tone="dim" class="tabular-nums">
          {`${plural(count().total, "source")} · ${plural(count().origins, "origin")}`}
        </Text>
      </Show>
    );
  },
  commands: (ctx) => {
    // **Failures first, and the total only where there are none.** A pane header has
    // room for one figure, and "3 failed" is the one that would make an operator open
    // it; the running total is what they read once they are already inside. The word
    // carries it as well as the tone, so the figure still says which kind it is when
    // the colour does not reach the reader (§12).
    const count = createMemo(() => {
      const all = ctx.viewport.commands();
      return { total: all.length, failed: failedCount(all) };
    });
    return (
      <Show when={count().total > 0}>
        <Text
          variant="micro"
          tone={count().failed > 0 ? "alert" : "dim"}
          class="tabular-nums"
        >
          {count().failed > 0 ? `${count().failed} failed` : `${count().total}`}
        </Text>
      </Show>
    );
  },
};
