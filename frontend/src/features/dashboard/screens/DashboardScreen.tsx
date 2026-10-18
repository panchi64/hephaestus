import { createMemo, createSignal, type JSX } from "solid-js";
import { useNavigate } from "@solidjs/router";
import { Composer, toast, type Status } from "~/ui";
import { overviewBand, useActiveRuns, useOverview } from "../data";
import type { CapabilityHealth } from "../model";
import { buildActivity, degraded, homeDigest } from "../activity";
import { pickGreeting } from "../greetings";
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

/** The launchpad as a desk: one column on a reading measure, holding the greeting plate,
 *  the composer, the activity ledger and the system footer, all on one left edge.
 *  The composer is the only framed object; everything else is bare (§10.2).
 *
 *  The screen's licensed moment (§11.1) is the dot field, drawn behind the shell's
 *  content column (`isDotFieldRoute`), centred where the composer lands. */
export function DashboardScreen(): JSX.Element {
  const navigate = useNavigate();
  const { data: overview, refetch: refetchOverview } = useOverview();
  const { data: runs } = useActiveRuns();
  const sessions = useChatSessions();
  // Files attached on the launchpad ride into the conversation's first turn.
  const attachments = createComposerAttachments();

  const greeting = pickGreeting(new Date(), readLS(GREETING_KEY));
  writeLS(GREETING_KEY, greeting);
  const today = DATE_FORMAT.format(new Date()).replaceAll(",", "");

  // `settled` throughout: `/runs` re-polls every 15s, and a tracked read of a
  // refetching resource would suspend the whole route on every poll.
  const caps = () => settled(overview)?.capabilities ?? [];
  const activity = createMemo(() =>
    buildActivity(sessions() ?? [], settled(runs) ?? [], ACTIVITY_LIMIT),
  );

  const digest = () => {
    const list = sessions() ?? [];
    const live = settled(runs) ?? [];
    return homeDigest({
      running: live.filter((r) => r.status !== "awaiting_input").length,
      waiting: list.filter((s) => s.activity === "awaiting_input").length,
      failed: activity().items.filter((i) => i.state === "error").length,
      critical: degraded(caps()).filter((c) => c.critical),
    });
  };

  const plateStatus = () => {
    const s = overallStatus(caps());
    if (s === "nominal") return undefined;
    const triggering = degraded(caps()).filter((c) =>
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
    return o ? `HEPH ${o.version} · ${today}` : undefined;
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
    // Centred vertically as one block, so the whitespace frames the desk evenly; a
    // long ledger simply fills the page and scrolls.
    <div class="mx-auto flex min-h-full w-full max-w-[880px] flex-col justify-center py-8">
      <HomePlate
        eyebrow={eyebrow()}
        greeting={greeting}
        digest={digest()}
        status={plateStatus()}
      />

      {/* The focal point, and the only framed object on the page. It always opens a
          new thread, so it carries no echo of an existing one's ask: those live in
          the ledger below. The dot field's falloff is centred where this lands
          (`.heph-dot-field` in dot-field.css); move the composer and move that too. */}
      <div class="mt-6">
        <Composer
          size="lg"
          title="New thread · ready"
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
          onRetry={refetchOverview}
          band={(o) =>
            overviewBand(o, selectedModelLabel(), effectiveContextWindow())
          }
        />
      </div>
    </div>
  );
}
