import {
  createEffect,
  createMemo,
  createSignal,
  untrack,
  type JSX,
} from "solid-js";
import { useNavigate } from "@solidjs/router";
import { Composer, toast, type Status } from "~/ui";
import { overviewBand, useActiveRuns, useOverview } from "../data";
import type { CapabilityHealth } from "../model";
import { buildActivity, degraded, homeDigest } from "../activity";
import { dayPartOf, pickGreeting } from "../greetings";
import { HomePlate } from "../components/HomePlate";
import { ActivityLedger } from "../components/ActivityLedger";
import { SystemFooter } from "../components/SystemFooter";
// The overview is a launchpad INTO chat, so it reads the chat feature's data seam
// directly (one source of truth for threads and entry intents). The model selection is
// global app state; the picker is the shared `ModelPicker`.
import {
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
import { stagedProjectId } from "~/lib/stores/stagedProject";
import { settled } from "~/lib/resource";
import { readLS, writeLS } from "~/lib/storage";
import { useNow } from "~/lib/useNow";

const ACTIVITY_LIMIT = 8;

const GREETING_KEY = "heph.home.greeting";

const DATE_FORMAT = new Intl.DateTimeFormat("en-GB", {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric",
});

/** Overall status for the plate's flag. Any down capability is an alert; a degraded
 *  *critical* one is a warning. The backend's `critical` flag is the severity policy,
 *  and this is the roll-up of it, not a second one. */
function overallStatus(caps: readonly CapabilityHealth[]): Status {
  if (caps.some((c) => c.status === "alert")) return "alert";
  if (caps.some((c) => c.critical && c.status === "warn")) return "warn";
  return "nominal";
}

/** The launchpad. */
export function DashboardScreen(): JSX.Element {
  const navigate = useNavigate();
  const { data: overview, refetch: refetchOverview } = useOverview();
  const { data: runs } = useActiveRuns();
  const sessions = useChatSessions();
  // Files attached on the launchpad ride into the conversation's first turn.
  const attachments = createComposerAttachments();

  // A minute is fine enough for the date, the time of day and the failure window, and
  // keeps a page left open overnight from showing yesterday.
  const now = useNow(60_000);
  const dayPart = createMemo(() => dayPartOf(new Date(now())));
  // Re-drawn only when the day part turns, so the line never changes under the eye.
  const greeting = createMemo(() => {
    void dayPart();
    return untrack(() => pickGreeting(new Date(now()), readLS(GREETING_KEY)));
  });
  createEffect(() => writeLS(GREETING_KEY, greeting()));
  const today = createMemo(() =>
    DATE_FORMAT.format(new Date(now())).replaceAll(",", ""),
  );

  // `settled` throughout: `/runs` re-polls every 15s, and a tracked read of a
  // refetching resource would suspend the whole route on every poll.
  const caps = () => settled(overview)?.capabilities ?? [];
  const lit = createMemo(() => degraded(caps()));
  const activity = createMemo(() =>
    buildActivity(sessions() ?? [], settled(runs) ?? [], ACTIVITY_LIMIT, now()),
  );

  const digest = () =>
    homeDigest({
      ...activity().counts,
      critical: lit().filter((c) => c.critical),
    });

  const plateStatus = () => {
    const s = overallStatus(caps());
    if (s === "nominal") return undefined;
    const triggering = lit().filter((c) =>
      s === "warn" ? c.critical : c.status === "alert",
    );
    return {
      tone: s as "warn" | "alert",
      label: s === "alert" ? "System alert" : "System warning",
      reason: triggering.map((c) => `${c.label}: ${c.detail}`).join(" · "),
    };
  };

  const eyebrow = () => {
    const o = settled(overview);
    return o ? `HEPH ${o.version} · ${today()}` : undefined;
  };

  /**
   * The launchpad's `/` menu. No `@` beside it: that browses a thread's own files, and
   * there is no thread here yet. The route withholds `compact`, `fork` and `retitle`
   * without a conversation; `level` is staged onto the handoff and applied as the
   * thread is created.
   */
  const commands = createComposerCommands(
    () => null,
    // The project staged for the next thread, so a repository's own `.claude/commands`
    // are offered here too — from the operator's checkout, since no worktree exists yet.
    () => stagedProjectId() ?? null,
    {
      // Unreachable without a conversation; wired so an unexpected id does nothing.
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
    // `/level auto` on its own is not a message: the level is staged and there is
    // nothing to open a thread with. Clearing the composer is the feedback.
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
    // Centred as one block; a long ledger fills the page and scrolls.
    <div class="mx-auto flex min-h-full w-full max-w-[880px] flex-col justify-center py-8">
      <HomePlate
        eyebrow={eyebrow()}
        greeting={greeting()}
        digest={digest()}
        status={plateStatus()}
      />

      {/* The focal point, and the only framed object on the page. It always opens a
          new thread, so it carries no echo of an existing one's ask: those live in
          the ledger below. */}
      <div class="mt-6">
        <Composer
          size="lg"
          title="Talos · ready"
          autofocus
          storageKey="home-new"
          placeholder="Ask anything, request a summary, or describe a task…"
          onSend={handleStart}
          attachments={attachments}
          menu={commands}
          sendBlocked={sendBlockedReason()}
          trailing={<ModelPicker />}
        />
      </div>

      <div class="mt-8">
        <ActivityLedger activity={activity()} onOpen={openThread} />
      </div>

      <div class="mt-8">
        <SystemFooter
          overview={overview}
          degraded={lit()}
          onRetry={refetchOverview}
          band={(o) =>
            overviewBand(o, selectedModelLabel(), effectiveContextWindow())
          }
        />
      </div>
    </div>
  );
}
