/** How a tool call reads in the transcript.
 *
 *  The agent's tools are namespaced `category_tool` (`backend/tools/catalog.py`),
 *  which is the name the model is offered and the name the operator's tool
 *  toggles match on — but it is a *registry* name, and a transcript row is not a
 *  registry. Reading `files_read_file, path=backend/app.py` costs a beat that
 *  "Read · backend/app.py" does not, and a turn is a dozen of those rows.
 *
 *  So each tool gets three things here: the glyph for its family (so a column of
 *  rows is scannable by shape before any word is read), a short label in the
 *  interface's own voice, and — where the generic fallbacks would guess wrong —
 *  which argument actually says what the call is about and what its results are
 *  counted in.
 *
 *  **The table is a display convenience, never a gate.** A tool missing from it
 *  still renders: it falls back to its category's glyph and a humanized form of
 *  its own name. That is what keeps `external_*` connector tools — which are
 *  discovered per operator and cannot be enumerated here — from needing an entry,
 *  and what keeps a newly landed backend tool from rendering as a blank row. */

import { sentenceCase } from "~/lib/format";
import type { IconName } from "~/ui";
import type { ToolInvocation } from "./model";

export interface ToolPresentation {
  icon: IconName;
  label: string;
}

export interface ToolEntry extends ToolPresentation {
  /** Argument keys in preference order; the first one present becomes the row's
   *  detail. Omitted where the generic order already picks the right one. */
  keys?: readonly string[];
  /** `[singular, plural]` for a counted result — "12 entries", "1 match". */
  noun?: readonly [string, string];
  /** Present when the call renders as a **terminal** rather than as a tool card: the
   *  command line, its approval, and its output as one continuous readout that never
   *  splits into an approval card and a separate result.
   *
   *  It is here, on the table, and not as a name test in the fold. Running a command
   *  is the thing the operator most needs to see happen, and there is more than one
   *  tool that does it — the fold used to ask `name === code_run_host_command` in four
   *  separate event cases, which is four places to remember when a second one lands,
   *  and is why `shell_run_command` (a worktree thread's only way to run anything, already
   *  carrying the terminal glyph two rows below) fell through to a generic card.
   *
   *  It used to say *how* the tool reported, because the worktree shell answered with
   *  one labelled string the client had to pull apart while the sandboxed one answered
   *  with a record. Both answer with a record now, so the distinction had one value
   *  left and no second reading — a flag is the honest shape of the question that
   *  remains. */
  terminal?: true;
  /** The argument that holds a program the call runs. The open card shows it as code
   *  and leaves it out of the argument dump, which would otherwise print the whole
   *  script as one `code=…` line. */
  script?: string;
}

/** The glyph for a category, for any tool without its own row below. Every
 *  registered category is listed, so a new tool lands in the right family on the
 *  day it lands rather than falling through to the generic plug. */
const CATEGORY_ICONS: Record<string, IconName> = {
  agents: "users",
  attachments: "attach",
  builtin: "clock",
  calendar: "calendar",
  code: "code",
  conversations: "chat",
  corpus: "library",
  external: "plug",
  files: "file",
  mail: "mail",
  memory: "database",
  plan: "note",
  project: "layers",
  repo: "branch",
  shell: "terminal",
  skills: "library",
  tasks: "note",
  vault: "lock",
  view: "panel-right",
  web: "search",
};

const DEFAULT_ICON: IconName = "plug";

const TOOLS: Record<string, ToolEntry> = {
  // Both keys, in this order: which agent is most of what the row says, and the task is
  // the part worth reading. The card is the *spawn* — an agent's own work is its card in
  // the Agents panel, not a line under the call that started it.
  agents_spawn: {
    icon: "users",
    label: "Sub-agent",
    keys: ["agent_name", "task"],
  },
  agents_message: {
    icon: "users",
    label: "Message agent",
    keys: ["to", "text"],
  },
  agents_list: { icon: "users", label: "List agents" },
  agents_read: { icon: "users", label: "Check agent", keys: ["name"] },
  agents_stop: { icon: "stop", label: "Stop agent", keys: ["name"] },

  attachments_provision: {
    icon: "attach",
    label: "Attach",
    keys: ["attachment_id"],
  },
  // The transcript's record of a question: the call is what remains once the dock has
  // been answered and dismissed, carrying both what was asked and what was said.
  builtin_ask_user: { icon: "chat", label: "Asked you" },
  builtin_now: { icon: "clock", label: "Clock" },

  calendar_agenda: {
    icon: "calendar",
    label: "Agenda",
    keys: ["start"],
    noun: ["event", "events"],
  },
  calendar_create_event: {
    icon: "calendar",
    label: "New event",
    keys: ["title"],
  },
  calendar_delete_event: {
    icon: "calendar",
    label: "Delete event",
    keys: ["event_id"],
  },
  calendar_list_calendars: {
    icon: "calendar",
    label: "Calendars",
    noun: ["calendar", "calendars"],
  },
  calendar_update_event: {
    icon: "calendar",
    label: "Edit event",
    keys: ["title", "event_id"],
  },

  code_execute: { icon: "code", label: "Run code", keys: ["code"] },
  code_run_host_command: {
    icon: "terminal",
    label: "Host command",
    keys: ["explanation", "command"],
    terminal: true,
  },

  conversations_read: {
    icon: "chat",
    label: "Read thread",
    keys: ["conversation_id"],
  },
  conversations_search: {
    icon: "chat",
    label: "Search threads",
    keys: ["query"],
    noun: ["thread", "threads"],
  },
  corpus_retrieve: {
    icon: "library",
    label: "Retrieve",
    keys: ["query"],
    noun: ["passage", "passages"],
  },

  files_create_directory: { icon: "file", label: "New folder" },
  files_edit_file: { icon: "edit", label: "Edit" },
  files_file_info: { icon: "file", label: "File info" },
  files_find_files: {
    icon: "search",
    label: "Find files",
    keys: ["pattern", "glob", "path"],
    noun: ["match", "matches"],
  },
  files_list_directory: {
    icon: "file",
    label: "List",
    noun: ["entry", "entries"],
  },
  files_read_file: { icon: "file", label: "Read" },
  files_search_files: {
    icon: "search",
    label: "Search files",
    keys: ["pattern", "query", "path"],
    noun: ["match", "matches"],
  },
  files_write_file: { icon: "pen", label: "Write" },

  mail_draft_reply: { icon: "pen", label: "Draft reply", keys: ["message_id"] },
  mail_list_accounts: {
    icon: "mail",
    label: "Mail accounts",
    noun: ["account", "accounts"],
  },
  mail_list_messages: {
    icon: "mail",
    label: "Inbox",
    keys: ["folder", "account_id"],
    noun: ["message", "messages"],
  },
  mail_mark: { icon: "mail", label: "Mark mail", keys: ["message_id"] },
  mail_read: { icon: "mail", label: "Read mail", keys: ["message_id"] },
  mail_reply: {
    icon: "send",
    label: "Reply",
    keys: ["explanation", "message_id"],
  },
  mail_send: { icon: "send", label: "Send mail", keys: ["subject", "to"] },

  memory_recall: {
    icon: "database",
    label: "Recall",
    keys: ["query"],
    noun: ["memory", "memories"],
  },
  memory_remember: { icon: "database", label: "Remember", keys: ["content"] },

  tasks_read: { icon: "note", label: "Read tasks", noun: ["task", "tasks"] },
  tasks_update_statuses: { icon: "note", label: "Task statuses" },
  tasks_write: { icon: "note", label: "Write tasks" },
  plan_enter: { icon: "note", label: "Enter plan mode" },
  plan_read: { icon: "note", label: "Read plan" },
  plan_submit: { icon: "note", label: "Submit plan" },

  project_active: { icon: "layers", label: "Active project" },
  project_list: {
    icon: "layers",
    label: "Projects",
    noun: ["project", "projects"],
  },
  project_open_worktree: { icon: "branch", label: "Opened a worktree" },
  repo_inventory_agent_context: { icon: "branch", label: "Repo context" },

  // A harness tool, like `search_tools` below, so it declares no category: a Python
  // script whose own tool calls are rows of their own, nested on this one's card. No
  // detail key — a script has no one argument that says what it is about, so the
  // narration leads and the open card shows the code.
  run_code: { icon: "code", label: "Script", keys: [], script: "code" },

  // The one row here the agent does not own: the harness offers this so a model
  // that has been handed only an index of the tool groups it is *not* carrying can
  // ask for one. It declares no category — `search` is not a namespace — so it gets
  // a glyph no family uses, which is also what it is: a row about the tool list
  // rather than about the work.
  search_tools: {
    icon: "grid",
    label: "Loading tools",
    keys: ["queries"],
    noun: ["tool", "tools"],
  },

  shell_check_command: { icon: "terminal", label: "Check process" },
  shell_run_command: {
    icon: "terminal",
    label: "Shell",
    keys: ["command"],
    terminal: true,
  },
  // Deliberately not a terminal: a background process is a handle the agent checks on
  // later, not a command whose output the operator watches arrive. Its result is that
  // handle, and a terminal showing one would be a terminal that never prints anything.
  shell_start_command: {
    icon: "terminal",
    label: "Start process",
    keys: ["command"],
  },
  shell_stop_command: { icon: "stop", label: "Stop process" },

  skills_create: { icon: "library", label: "New skill", keys: ["name"] },
  skills_edit: {
    icon: "library",
    label: "Edit skill",
    keys: ["name", "explanation"],
  },
  skills_open: { icon: "library", label: "Open skill", keys: ["name"] },

  vault_get_entry: { icon: "key", label: "Secret", keys: ["entry_id"] },
  vault_list_entries: {
    icon: "lock",
    label: "Vault",
    keys: ["reason"],
    noun: ["entry", "entries"],
  },

  view_close: { icon: "close", label: "Close view" },
  view_show: {
    icon: "panel-right",
    label: "Show view",
    keys: ["title", "file", "path"],
  },

  web_fetch: { icon: "link", label: "Fetch", keys: ["url"] },
  web_search: {
    icon: "search",
    label: "Web search",
    keys: ["query"],
    noun: ["result", "results"],
  },
};

/** The namespace a name declares, or `""` for a name that declares none. The
 *  guard is the point: `slice(0, indexOf(…))` on a name with no underscore takes
 *  `slice(0, -1)`, which hands back the name minus its last character — and
 *  `"views"` would then read as the `view` category, taking that glyph and
 *  losing its own label to the prefix strip below. */
function categoryOf(name: string): string {
  const cut = name.indexOf("_");
  return cut === -1 ? "" : name.slice(0, cut);
}

/** `files_read_file` → `Read file`; `external_linear_create_issue` → `Linear
 *  create issue`. Drops a *known* category prefix only — an unrecognized name is
 *  humanized whole rather than losing its first word to a bad guess.
 *
 *  The sentence-casing itself is `lib/format`'s, shared with the context breakdown's
 *  row label: both are "a backend slug nobody wrote a label for", and the only thing
 *  this adds is the prefix strip. */
function humanize(name: string): string {
  const category = categoryOf(name);
  const bare =
    category in CATEGORY_ICONS ? name.slice(category.length + 1) : name;
  // The full registry name is the fallback, not the stripped remainder: `files_` has
  // nothing left after the prefix, and a blank row says less than the raw name does.
  return sentenceCase(bare, name);
}

/** The full row for a tool — presentation plus the summarizer's hints. */
export function toolEntry(name: string): ToolEntry {
  const known = TOOLS[name];
  if (known) return known;
  return {
    icon: CATEGORY_ICONS[categoryOf(name)] ?? DEFAULT_ICON,
    label: humanize(name),
  };
}

/** Whether this tool renders as a terminal. The single question the live fold and the
 *  cold mapper both ask, so the two cannot disagree about whether a reload turns a
 *  terminal back into a tool card. */
export function isTerminalTool(name: string): boolean {
  return toolEntry(name).terminal === true;
}

/** The glyph and label a transcript row leads with. */
export function toolPresentation(name: string): ToolPresentation {
  const { icon, label } = toolEntry(name);
  return { icon, label };
}

/** A call as one line of text — "Read · backend/app.py". What the tool card's own
 *  header says, for the places that have room for a line but not a card. */
export function toolRowLabel(tool: ToolInvocation): string {
  const { label } = toolEntry(tool.name);
  const detail = tool.detail ?? tool.args;
  return detail ? `${label} · ${detail}` : label;
}
