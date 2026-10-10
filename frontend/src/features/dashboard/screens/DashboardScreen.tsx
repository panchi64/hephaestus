import { For, Show, createMemo, createSignal, type JSX } from "solid-js";
import { useNavigate } from "@solidjs/router";
import {
  AnnunciatorGrid,
  Composer,
  ConsoleGroup,
  EmptyState,
  ListRow,
  MetClock,
  PageHeader,
  Panel,
  Resource,
  StatusFlag,
  Text,
  Tooltip,
  type Status,
} from "~/ui";
import { overviewBand, useActiveRuns, useOverview } from "../data";
import type { ActiveRun, CapabilityHealth } from "../model";
import { RecentThreadCard } from "../components/RecentThreadCard";
import { SystemStrip } from "../components/SystemStrip";
// The overview is a launchpad INTO chat, so it reads the chat feature's data
// seam directly (one source of truth for threads and entry intents). The model
// selection is global app state — the picker itself is the shared `ModelPicker`,
// so this screen reads the selection but never renders its own control for it.
import {
  entrySessionId,
  openConversation,
  startConversation,
  useChatSessions,
} from "~/features/chat/data";
import {
  effectiveContextWindow,
  sendBlockedReason,
  effectiveSelection,
  selectedModelLabel,
} from "~/lib/stores/models";
import { ModelPicker } from "~/app/ModelPicker";
import { createComposerAttachments } from "~/features/uploads/data";
import { createComposerCommands } from "~/features/chat/commands/useComposerCommands";
import {
  parsePermissionLevel,
  type PermissionLevel,
} from "~/features/chat/model";
import { activeSessionMode, codeProjectId } from "~/lib/stores/sessionMode";
import { toast } from "~/ui";
import { settled } from "~/lib/resource";

/** Overall status for the header flag. Any down capability is an alert; a
 *  degraded *critical* capability is a warning. Non-critical degradations
 *  (e.g. keyword-only recall) stay off the top-level flag but still light their
 *  own cell on the annunciator grid — the backend's `critical` flag is the
 *  severity policy, and this flag is the roll-up of it, not a second one. */
function computeOverallStatus(caps: CapabilityHealth[]): Status {
  if (caps.some((c) => c.status === "alert")) return "alert";
  if (caps.some((c) => c.critical && c.status === "warn")) return "warn";
  return "nominal";
}

/** The annunciator panel's own readout: how many cells are lit, or that none are.
 *
 *  Phrased as a count of what is *wrong* rather than of what is fine, because that is
 *  the question the panel exists to answer — and "All nominal" is the one case where
 *  a word beats a number, since zero of something bad is not a quantity anyone reads. */
function annunciatorSummary(caps: CapabilityHealth[]): string {
  const lit = caps.filter(
    (c) => c.status === "warn" || c.status === "alert",
  ).length;
  return lit ? `${lit} annunciated` : "All nominal";
}

const RECENT_LIMIT = 6;

/** Status → chip tone for an in-flight run. A parked run awaiting the operator's
 *  decision gets the warn accent — the others read as plain ambient activity. */
const RUN_STATUS_TONE: Record<ActiveRun["status"], Status> = {
  running: "info",
  queued: "idle",
  awaiting_input: "warn",
};

/** Home overview as a launchpad: a centered composer to start work, recent threads to
 *  resume it, the event sequencer, the caution & warning panel, and a subtle facts
 *  strip. Every panel reflects real backend state — the composer/threads via the chat
 *  seam, the facts band + capability health via `/overview`, the sequencer via `/runs`.
 *
 *  The screen's **licensed moment** (§11.1) is the deep field, and this screen does not
 *  draw it: `isDeepFieldRoute` puts it behind the shell's whole content column, because
 *  cropping it to one region cut the limb off short of the page edges it is drawn
 *  against. What this screen owes the field is the other half of the arrangement —
 *  every surface here either frosts it out (`ody-framed` → glass) or has no surface at
 *  all, so nothing on the launchpad is read against a graticule. */
export function DashboardScreen(): JSX.Element {
  const navigate = useNavigate();
  const { data: overview, refetch: refetchOverview } = useOverview();
  const { data: runs } = useActiveRuns();
  const sessions = useChatSessions();
  // Files attached on the launchpad ride into the conversation's first turn.
  const attachments = createComposerAttachments();

  // The resume target: the newest still-warm thread (or none).
  const entryId = createMemo(() => {
    const list = sessions();
    return list ? entrySessionId(list) : null;
  });
  const recent = createMemo(() => sessions()?.slice(0, RECENT_LIMIT) ?? []);
  // The sequencer's header count. `settled` reads the resource WITHOUT suspending —
  // this is rendered outside the `Resource` boundary that guards the list itself, and
  // `/runs` re-polls every 15s, so a tracked `runs()` here would throw the whole route
  // to the shell's fallback on every poll.
  const activeCount = () => settled(runs)?.length ?? 0;

  const overallStatus = (): Status => {
    const o = overview();
    return o ? computeOverallStatus(o.capabilities) : "nominal";
  };
  const overallLabel = (): string => {
    const s = overallStatus();
    if (s === "alert") return "System alert";
    if (s === "warn") return "System warning";
    return "All systems";
  };

  // The capabilities responsible for the current flag — the same severity policy
  // as `computeOverallStatus`: down capabilities raise an alert, degraded
  // *critical* ones a warning. Their details become the flag's hover tooltip so
  // the operator sees *what* is wrong without leaving the launchpad.
  const alertReason = (): string | null => {
    const o = overview();
    if (!o) return null;
    const s = overallStatus();
    const triggering =
      s === "alert"
        ? o.capabilities.filter((c) => c.status === "alert")
        : s === "warn"
          ? o.capabilities.filter((c) => c.critical && c.status === "warn")
          : [];
    if (!triggering.length) return null;
    return triggering.map((c) => `${c.label}: ${c.detail}`).join(" · ");
  };

  /**
   * The launchpad's `/` menu. No `@` beside it: that browses a thread's own worktree,
   * and there is no thread here yet.
   *
   * Three of the five built-in actions are absent without the picker doing anything —
   * the route drops `compact`, `fork` and `retitle` when it is told there is no
   * conversation, which is the whole reason availability is decided there. Of the two
   * that remain, `new` is what this screen already *is*, and `level` has nowhere to write
   * until a thread exists — so it is staged onto the handoff and applied as the thread is
   * created, which is what its own description already promises.
   */
  const commands = createComposerCommands(
    activeSessionMode,
    () => null,
    // The project staged in the rail for the next code thread, so a repository's own
    // `.claude/commands` are offered here too — from the operator's checkout, since the
    // worktree that thread will work in does not exist yet.
    () => codeProjectId() ?? null,
    {
      // Unreachable — the route withholds all three without a conversation. They are
      // wired rather than left to throw, because "what this build does with an id it was
      // offered" should not depend on which ids the backend happens to send today.
      compact: () => {},
      fork: () => {},
      retitle: () => {},
      // Already here: an empty composer on the launchpad is a new thread.
      newThread: () => {},
      setPermissionLevel: (level) => {
        const parsed = parsePermissionLevel(level);
        if (parsed) setPendingLevel(parsed);
        else toast.error(`"${level}" isn't a permission level.`);
      },
    },
  );
  const [pendingLevel, setPendingLevel] = createSignal<
    PermissionLevel | undefined
  >();

  const handleStart = (text: string, attachmentIds: string[]) => {
    const intent = commands.consume(text);
    // `/level auto` on its own is not a message: the relay has run (the level is staged)
    // and there is nothing to open a thread with. Clearing the composer is the feedback.
    if (intent.kind === "acted") return;
    startConversation(text, effectiveSelection(), attachmentIds, {
      command: intent.command,
      permissionLevel: pendingLevel(),
    });
    navigate("/chat");
  };
  const openThread = (id: string) => {
    openConversation(id);
    navigate("/chat");
  };

  return (
    <div class="flex min-h-full flex-col gap-6">
      <PageHeader
        title="Odysseus"
        subtitle="Your private, self-hosted AI workspace — chat, code, research, memory, and more."
        assetId="ODY-HUD-00.1 EDITION 02"
        actions={
          <Show
            when={alertReason()}
            fallback={
              <StatusFlag status={overallStatus()} dot>
                {overallLabel()}
              </StatusFlag>
            }
          >
            {(reason) => (
              <Tooltip label={reason()} side="left">
                <StatusFlag status={overallStatus()} dot>
                  {overallLabel()}
                </StatusFlag>
              </Tooltip>
            )}
          </Show>
        }
      />

      {/* Composer — the focal point, vertically centered in the free space. It
          is the only card on this screen that lights its accent on focus, which
          is what makes "start typing" the obvious move on arrival (§6.2).

          The deep field runs behind the whole column now (the shell paints it), so
          there is no cropping layer here any more — one lived in this region and
          sliced the field to a 320px band. The composer's own surface goes glass
          over it. */}
      <div class="flex min-h-0 flex-1 items-center justify-center py-8">
        <div class="w-full max-w-3xl">
          <Composer
            size="lg"
            title="New conversation"
            autofocus
            storageKey="home-new"
            placeholder="Ask anything, request a summary, or describe a task…"
            onSend={handleStart}
            attachments={attachments}
            menu={commands}
            sendBlocked={sendBlockedReason()}
            // Same slot, same component as the docked composer in a room: the
            // launchpad's picker was a second inline copy of the shared one, and two
            // copies of a control bound to one backend value is how they drift.
            trailing={<ModelPicker />}
          />
        </div>
      </div>

      {/* Bottom-aligned: recent threads + in-flight, then the system strip. */}
      <div class="flex flex-col gap-4">
        <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
          {/* Recent threads — the launchpad's navigation; default brightness. */}
          <Panel label="Recent threads" bare class="lg:col-span-2">
            <Show
              when={recent().length}
              fallback={
                <EmptyState
                  icon="terminal"
                  message="No conversations yet"
                  hint="Start one above to see it here."
                />
              }
            >
              <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <For each={recent()}>
                  {(s) => (
                    <RecentThreadCard
                      title={s.title}
                      preview={s.preview}
                      model={s.model}
                      updatedAt={s.updatedAt}
                      warm={s.id === entryId()}
                      onOpen={() => openThread(s.id)}
                    />
                  )}
                </For>
              </div>
            </Show>
          </Panel>

          {/* The event sequencer (§10.14): runs as numbered mission events against
              the clock, rather than an undifferentiated list. Each row is designated
              by its run id — there is deliberately no SEQ ordinal, because the
              backend assigns none and an array index would be this layer inventing
              state it does not own. */}
          <ConsoleGroup
            label="Sequence"
            class="lg:col-span-1"
            right={
              <Text variant="plate" tone="dim">
                {/* `settled`, never `runs()`: a tracked read of a refetching resource
                    suspends the nearest boundary, which here is the shell's around the
                    whole route — and this list re-polls every 15s. Same rule `Resource`
                    itself follows for the body below. */}
                {activeCount() ? `${activeCount()} active` : "—"}
              </Text>
            }
          >
            <Resource
              data={runs}
              emptyMessage="No active runs"
              isEmpty={(r) => r.length === 0}
            >
              {(list) => (
                <For each={list()}>
                  {(run) => {
                    // A run with no linked conversation (a stateless one) renders
                    // as a plain row — nothing to navigate to.
                    const clickable = () => !!run.conversationId;
                    return (
                      <ListRow
                        label={
                          <span class="flex min-w-0 items-center gap-2">
                            {/* A queued run has no start time yet, so its clock
                                reads `--:--:--`. That is the honest state: it is
                                waiting, not running fast. */}
                            <MetClock
                              startedAt={run.startedAt}
                              endedAt={run.endedAt}
                              variant="micro"
                              prefix={false}
                              class="shrink-0"
                            />
                            {/* The run's own tag (CHAT / AGENT). It is the only
                                thing distinguishing a stateless agent run from a
                                chat one, and a stateless run has no conversation to
                                click through to either. */}
                            <Text variant="plate" tone="dim" class="shrink-0">
                              {run.kind}
                            </Text>
                            <Text variant="micro" tone="dim" class="truncate">
                              {run.label}
                            </Text>
                          </span>
                        }
                        onClick={
                          clickable()
                            ? () => openThread(run.conversationId!)
                            : undefined
                        }
                        right={
                          <StatusFlag status={RUN_STATUS_TONE[run.status]}>
                            {run.detail}
                          </StatusFlag>
                        }
                      />
                    );
                  }}
                </For>
              )}
            </Resource>
          </ConsoleGroup>
        </div>

        {/* Both panels below read `/overview`, so they share ONE `Resource`. Two of
            them over one resource renders its loading arm twice and, on failure, two
            stacked error rows with two retry buttons for a single fetch. */}
        <Resource
          data={overview}
          onRetry={refetchOverview}
          errorMessage="Telemetry unavailable"
        >
          {(o) => (
            <>
              {/* Caution & warning (§10.15) — the capability health that used to be a
                  row of equally-bright dots on the system strip. */}
              <ConsoleGroup
                label="Caution & warning"
                right={
                  <Text variant="plate" tone="dim">
                    {annunciatorSummary(o().capabilities)}
                  </Text>
                }
              >
                <AnnunciatorGrid
                  cells={o().capabilities.map((c) => ({
                    label: c.label,
                    status: c.status,
                    detail: c.detail,
                    href: c.remediationHref,
                  }))}
                />
              </ConsoleGroup>

              {/* System strip — most subtle; compact, marquees only if it overflows.
                  Facts only now: anything that can be *wrong* is on the annunciator
                  grid above, where it can be seen rather than skimmed past. */}
              <SystemStrip
                band={overviewBand(
                  o(),
                  selectedModelLabel(),
                  effectiveContextWindow(),
                )}
              />
            </>
          )}
        </Resource>
      </div>
    </div>
  );
}
