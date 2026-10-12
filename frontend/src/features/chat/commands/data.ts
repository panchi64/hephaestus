/** The command catalog seam — one read, mapped, and nothing decided here.
 *
 *  Filtering and ranking are deliberately absent. The backend narrows the catalog on
 *  facts only it holds (a withheld toolset, a thread that does not exist yet) and the
 *  *matching* of a typed query against it is the one thing left — which stays here only
 *  because it is presentation, and is written as such: a plain, testable ranking over
 *  rows that have already been decided.
 */

import { createResource, type Resource } from "solid-js";
import { api } from "~/lib/api";
import type { CommandCatalog } from "./model";

interface CatalogOut {
  groups: { id: string; label: string; order: number }[];
  commands: {
    name: string;
    qualifiedName: string;
    group: string;
    title: string;
    description: string;
    argumentHint: string | null;
    kind: string;
    actionId: string | null;
    actionArgument: { required: boolean; choices: string[] } | null;
    shadowedBy: string | null;
  }[];
}

function toCatalog(dto: CatalogOut): CommandCatalog {
  return {
    groups: dto.groups,
    commands: dto.commands.map((row) => ({
      ...row,
      kind: row.kind as CommandCatalog["commands"][number]["kind"],
      actionId: row.actionId as CommandCatalog["commands"][number]["actionId"],
    })),
  };
}

async function fetchCatalog(
  key: readonly [string | null, string | null],
): Promise<CommandCatalog> {
  const [conversationId, projectId] = key;
  const query = new URLSearchParams();
  // Presence *and* identity, for two different questions. The backend drops the actions
  // that need a thread to act on when there is none, so a launchpad composer is simply
  // offered fewer rows — that part is presence. It is also what says which worktree the
  // project's own `.claude/commands` are read from, since a worktree thread works in a
  // branch cut from the project rather than in the operator's checkout.
  if (conversationId) query.set("conversation_id", conversationId);
  // And which project declared them at all. Sent separately because a thread's binding
  // is settled at creation: before then, the composer is the only thing that knows.
  if (projectId) query.set("project_id", projectId);
  return toCatalog(await api.get<CatalogOut>(`/commands?${query}`));
}

/** The commands offerable in this composer. Re-reads when the thread or the project
 *  changes, since both narrow what the backend will offer. */
export function useCommands(
  conversationId: () => string | null,
  projectId: () => string | null,
): Resource<CommandCatalog> {
  const [data] = createResource(
    () => [conversationId(), projectId()] as const,
    fetchCatalog,
  );
  return data;
}
