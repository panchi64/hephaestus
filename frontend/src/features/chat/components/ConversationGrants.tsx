import { createResource, For, Show, type JSX } from "solid-js";
import { Chip, confirm, Icon, toast } from "~/ui";
import { fetchGrants, revokeGrant } from "../data";
import { settled } from "~/lib/resource";
import { StatRow } from "./StatRow";
import type { ApprovalGrant } from "../model";

/** The conversation's active auto-approval grants — what the operator allowed to
 *  skip the per-call approval prompt for the rest of this thread, each named by the act it
 *  covers (a command where the tool runs one, else the tool). Renders nothing when
 *  there are none, and carries no band of its own: it is one row of the conversation's
 *  stats panel, which owns the layout.
 *
 *  **It lives behind Stats, not on the row under the composer.** It was a count on that
 *  line with the chips one popover further away, and before that a run of chips inline —
 *  the widest thing the old status band carried. Either way it was a per-thread setting
 *  parked among the glance items, and the line it sat in is now kept for what changes
 *  turn to turn. In the panel the operator has already opened, the chips can sit in the
 *  open: that *is* the place you go when "am I still auto-approving anything?" matters.
 *
 *  Refetches when the thread changes or the `revalidate` accessor ticks (e.g. a grant
 *  was just recorded). */
export function ConversationGrants(props: {
  conversationId: () => string | null;
  revalidate?: () => unknown;
}): JSX.Element {
  // Tag the fetched grants with the conversation they belong to. On a thread switch the
  // resource keeps the *previous* thread's value until the refetch resolves; without the
  // tag the panel would show stale chips and a click would revoke against the now-current
  // (wrong) conversation. The tag lets us ignore the value until it matches what's on
  // screen, and revoke against the conversation the chips actually belong to.
  //
  // The fetcher swallows its own failure rather than rejecting. This row is a
  // secondary read in a portalled panel, outside the transcript's ErrorBoundary: a
  // rejected resource re-throws on read and would take the whole chat screen down
  // with it. An unreachable grants endpoint should cost the operator the row, nothing
  // more — the next decision re-ticks `revalidate` and it comes back.
  const [grants, { mutate, refetch }] = createResource(
    () => ({ id: props.conversationId(), tick: props.revalidate?.() }),
    async (src) => ({
      id: src.id,
      items: src.id
        ? await fetchGrants(src.id).catch(() => [] as ApprovalGrant[])
        : ([] as ApprovalGrant[]),
    }),
  );

  // `settled`, not the resource — reading it while pending would suspend the
  // content region on every thread switch.
  const current = () => {
    const g = settled(grants);
    return g && g.id === props.conversationId() ? g : undefined;
  };
  const items = (): ApprovalGrant[] => current()?.items ?? [];

  // What a grant is *called* — the command it names where it has one, else the tool.
  // A tool that runs a command holds one grant per act, so the tool name alone would put
  // two indistinguishable chips side by side and revoke the wrong one. Joining the words
  // is presentation and stops here: the revoke sends the words themselves.
  // A scope recorded under a wider reach leads with `@host` / `@network` (the backend's
  // marker), which reads better as a suffix: "brew install wget (host)".
  // A whole-tool grant on a command-running tool says so, rather than sitting under a bare
  // tool name: it is the chip the operator is most likely to want to revoke. Only there,
  // though — on a tool that runs no command "any command" would name commands it never runs.
  const wholeTool = (g: ApprovalGrant) =>
    g.commandScoped && g.commandPrefix.length === 0;
  const label = (g: ApprovalGrant) => {
    const [head, ...rest] = g.commandPrefix;
    if (head?.startsWith("@")) return `${rest.join(" ")} (${head.slice(1)})`;
    if (g.commandPrefix.length) return g.commandPrefix.join(" ");
    return wholeTool(g) ? `${g.toolName} · any command` : g.toolName;
  };

  // Which grant a row *is*, for the optimistic removal below. Compared word by word rather
  // than through the label, so two scopes cannot collapse into one on the way to a string.
  const isSame = (a: ApprovalGrant, b: ApprovalGrant) =>
    a.toolName === b.toolName &&
    a.commandPrefix.length === b.commandPrefix.length &&
    a.commandPrefix.every((word, i) => word === b.commandPrefix[i]);

  async function revoke(grant: ApprovalGrant) {
    const id = current()?.id;
    if (!id) return;
    const name = label(grant);
    const ok = await confirm({
      title: `Stop auto-approving ${name}?`,
      detail: grant.commandPrefix.length
        ? "The agent will pause and ask for approval the next time it runs this command in this conversation."
        : wholeTool(grant)
          ? "The agent will pause and ask for approval the next time it runs anything with this tool in this conversation."
          : "The agent will pause and ask for approval the next time it calls this tool in this conversation.",
      confirmLabel: "Revoke",
      cancelLabel: "Cancel",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await revokeGrant(id, grant.toolName, grant.commandPrefix);
      mutate((g) =>
        g
          ? {
              ...g,
              items: g.items.filter((x) => !isSame(x, grant)),
            }
          : g,
      );
      toast.success(`Stopped auto-approving ${name}.`);
    } catch {
      toast.error("Unable to revoke the grant.");
      void refetch();
    }
  }

  return (
    <Show when={items().length > 0}>
      <StatRow
        label="Auto-approved"
        value={items().length}
        hint="Tools and commands you allowed to run without asking again in this conversation. Click one to stop auto-approving it."
      >
        <div class="flex flex-wrap gap-2">
          <For each={items()}>
            {(g) => (
              <Chip leading="check" onClick={() => revoke(g)}>
                <span class="inline-flex items-center gap-1">
                  {label(g)}
                  <Icon name="close" size={12} />
                </span>
              </Chip>
            )}
          </For>
        </div>
      </StatRow>
    </Show>
  );
}
