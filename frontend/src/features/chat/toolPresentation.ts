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
  /** What the row says while the model is still writing the call's arguments. */
  preparing?: string;
}

/** The glyph for a category, for any tool without its own row below. Every
 *  registered category is listed, so a new tool lands in the right family on the
 *  day it lands rather than falling through to the generic plug. */
const CATEGORY_ICONS: Record<string, IconName> = {
  agents: "users",
  attachments: "attach",
  browse: "eye",
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

/** The preparing phrase for any tool without its own row, by category. */
const CATEGORY_PREPARING: Record<string, string> = {
  agents: "Briefing a sub-agent",
  attachments: "Preparing an attachment",
  browse: "Preparing a browser action",
  builtin: "Preparing a built-in call",
  calendar: "Preparing a calendar change",
  code: "Writing code",
  conversations: "Preparing a conversation lookup",
  corpus: "Composing a corpus query",
  external: "Preparing a connector call",
  files: "Preparing a file operation",
  mail: "Preparing a mail action",
  memory: "Preparing a memory change",
  plan: "Preparing a plan step",
  project: "Preparing a project action",
  repo: "Preparing a repo lookup",
  shell: "Composing a shell command",
  skills: "Preparing a skill change",
  tasks: "Preparing a task update",
  vault: "Preparing a vault request",
  view: "Preparing a view",
  web: "Preparing a web request",
};

const DEFAULT_ICON: IconName = "plug";

export const TOOLS: Record<string, ToolEntry> = {
  // Both keys, in this order: which agent is most of what the row says, and the task is
  // the part worth reading. The card is the *spawn* — an agent's own work is its card in
  // the Agents panel, not a line under the call that started it.
  agents_spawn: {
    icon: "users",
    label: "Sub-agent",
    keys: ["agent_name", "task"],
    preparing: "Briefing a sub-agent",
  },
  agents_message: {
    icon: "users",
    label: "Message agent",
    keys: ["to", "text"],
    preparing: "Writing to a sub-agent",
  },
  agents_list: {
    icon: "users",
    label: "List agents",
    preparing: "Listing the sub-agents",
  },
  agents_read: {
    icon: "users",
    label: "Check agent",
    keys: ["name"],
    preparing: "Checking on a sub-agent",
  },
  agents_stop: {
    icon: "stop",
    label: "Stop agent",
    keys: ["name"],
    preparing: "Stopping a sub-agent",
  },
  agents_merge: {
    icon: "branch",
    label: "Merge agent's work",
    keys: ["name"],
    preparing: "Merging a sub-agent's work",
  },
  agents_discard: {
    icon: "trash",
    label: "Discard agent's work",
    keys: ["name"],
    preparing: "Discarding a sub-agent's work",
  },

  attachments_provision: {
    icon: "attach",
    label: "Attach",
    keys: ["attachment_id"],
    preparing: "Attaching a file",
  },

  browse_navigate: {
    icon: "link",
    label: "Navigate",
    keys: ["url"],
    preparing: "Choosing a page to open",
  },
  browse_snapshot: {
    icon: "eye",
    label: "Snapshot",
    preparing: "Reading the page structure",
  },
  browse_click: {
    icon: "reticle",
    label: "Click",
    preparing: "Aiming a click",
  },
  browse_type_text: {
    icon: "pen",
    label: "Type",
    preparing: "Composing text to type",
  },
  browse_press_key: {
    icon: "pen",
    label: "Press key",
    preparing: "Picking a key to press",
  },
  browse_select_option: {
    icon: "filter",
    label: "Select option",
    preparing: "Choosing a menu option",
  },
  browse_hover: {
    icon: "reticle",
    label: "Hover",
    preparing: "Aiming a hover",
  },
  browse_wait_for: {
    icon: "clock",
    label: "Wait for",
    preparing: "Setting what to wait for",
  },
  browse_screenshot: {
    icon: "image",
    label: "Screenshot",
    preparing: "Framing a screenshot",
  },
  browse_get_text: {
    icon: "eye",
    label: "Page text",
    preparing: "Choosing text to read",
  },
  browse_scroll: {
    icon: "eye",
    label: "Scroll",
    preparing: "Planning a scroll",
  },
  browse_go_back: {
    icon: "chevron-left",
    label: "Go back",
    preparing: "Heading back a page",
  },
  browse_go_forward: {
    icon: "chevron-right",
    label: "Go forward",
    preparing: "Heading forward a page",
  },
  browse_execute_js: {
    icon: "code",
    label: "Page script",
    keys: ["code", "script"],
    preparing: "Writing a page script",
  },
  browse_tabs: {
    icon: "panel-right",
    label: "Tabs",
    preparing: "Choosing a tab action",
  },
  browse_handle_next_dialog: {
    icon: "warning",
    label: "Handle dialog",
    preparing: "Deciding how to answer a dialog",
  },
  browse_console_messages: {
    icon: "terminal",
    label: "Console",
    preparing: "Asking for the page console",
  },
  browse_network_requests: {
    icon: "activity",
    label: "Network",
    preparing: "Asking for network requests",
  },

  // The transcript's record of a question: the call is what remains once the dock has
  // been answered and dismissed, carrying both what was asked and what was said.
  builtin_ask_user: {
    icon: "chat",
    label: "Asked you",
    preparing: "Phrasing a question for you",
  },
  builtin_now: {
    icon: "clock",
    label: "Clock",
    preparing: "Checking the clock",
  },

  calendar_agenda: {
    icon: "calendar",
    label: "Agenda",
    keys: ["start"],
    noun: ["event", "events"],
    preparing: "Choosing an agenda range",
  },
  calendar_create_event: {
    icon: "calendar",
    label: "New event",
    keys: ["title"],
    preparing: "Drafting a calendar event",
  },
  calendar_delete_event: {
    icon: "calendar",
    label: "Delete event",
    keys: ["event_id"],
    preparing: "Picking an event to delete",
  },
  calendar_list_calendars: {
    icon: "calendar",
    label: "Calendars",
    noun: ["calendar", "calendars"],
    preparing: "Listing the calendars",
  },
  calendar_update_event: {
    icon: "calendar",
    label: "Edit event",
    keys: ["title", "event_id"],
    preparing: "Revising a calendar event",
  },

  code_execute: {
    icon: "code",
    label: "Run code",
    keys: ["code"],
    preparing: "Writing code to run",
  },
  code_run_host_command: {
    icon: "terminal",
    label: "Host command",
    keys: ["explanation", "command"],
    terminal: true,
    preparing: "Composing a host command",
  },

  corpus_retrieve: {
    icon: "library",
    label: "Retrieve",
    keys: ["query"],
    noun: ["passage", "passages"],
    preparing: "Composing a corpus query",
  },

  files_create_directory: {
    icon: "file",
    label: "New folder",
    preparing: "Naming a new folder",
  },
  files_edit_file: {
    icon: "edit",
    label: "Edit",
    preparing: "Writing an edit",
  },
  files_file_info: {
    icon: "file",
    label: "File info",
    preparing: "Asking about a file",
  },
  files_find_files: {
    icon: "search",
    label: "Find files",
    keys: ["pattern", "glob", "path"],
    noun: ["match", "matches"],
    preparing: "Writing a file pattern",
  },
  files_list_directory: {
    icon: "file",
    label: "List",
    noun: ["entry", "entries"],
    preparing: "Choosing a folder to list",
  },
  files_read_file: {
    icon: "file",
    label: "Read",
    preparing: "Reading a file",
  },
  files_search_files: {
    icon: "search",
    label: "Search files",
    keys: ["pattern", "query", "path"],
    noun: ["match", "matches"],
    preparing: "Composing a file search",
  },
  files_write_file: {
    icon: "pen",
    label: "Write",
    preparing: "Writing a file",
  },

  mail_draft_reply: {
    icon: "pen",
    label: "Draft reply",
    keys: ["message_id"],
    preparing: "Drafting a reply",
  },
  mail_list_accounts: {
    icon: "mail",
    label: "Mail accounts",
    noun: ["account", "accounts"],
    preparing: "Listing the mail accounts",
  },
  mail_list_messages: {
    icon: "mail",
    label: "Inbox",
    keys: ["folder", "account_id"],
    noun: ["message", "messages"],
    preparing: "Choosing a mailbox to list",
  },
  mail_mark: {
    icon: "mail",
    label: "Mark mail",
    keys: ["message_id"],
    preparing: "Marking a message",
  },
  mail_read: {
    icon: "mail",
    label: "Read mail",
    keys: ["message_id"],
    preparing: "Opening a message",
  },
  mail_reply: {
    icon: "send",
    label: "Reply",
    keys: ["explanation", "message_id"],
    preparing: "Writing a reply",
  },
  mail_send: {
    icon: "send",
    label: "Send mail",
    keys: ["subject", "to"],
    preparing: "Writing an email",
  },

  memory_recall: {
    icon: "database",
    label: "Recall",
    keys: ["query"],
    noun: ["memory", "memories"],
    preparing: "Composing a memory query",
  },
  memory_remember: {
    icon: "database",
    label: "Remember",
    keys: ["description", "content"],
    preparing: "Putting a memory into words",
  },
  memory_open: {
    icon: "database",
    label: "Open memory",
    keys: ["id"],
    preparing: "Picking a memory to open",
  },
  memory_update: {
    icon: "database",
    label: "Update memory",
    keys: ["description", "id"],
    preparing: "Rewording a memory",
  },
  memory_list: {
    icon: "database",
    label: "List memories",
    noun: ["memory", "memories"],
    preparing: "Listing the memories",
  },
  memory_audit: {
    icon: "database",
    label: "Audit memory",
    noun: ["group", "groups"],
    preparing: "Setting up a memory audit",
  },
  memory_forget: {
    icon: "database",
    label: "Forget memory",
    keys: ["id"],
    preparing: "Picking a memory to forget",
  },
  memory_consolidate: {
    icon: "database",
    label: "Merge memories",
    keys: ["description", "keep"],
    preparing: "Merging memories into one",
  },

  tasks_read: {
    icon: "note",
    label: "Read tasks",
    noun: ["task", "tasks"],
    preparing: "Opening the task list",
  },
  tasks_update_statuses: {
    icon: "note",
    label: "Task statuses",
    preparing: "Updating task statuses",
  },
  tasks_write: {
    icon: "note",
    label: "Write tasks",
    preparing: "Writing the task list",
  },
  plan_enter: {
    icon: "note",
    label: "Enter plan mode",
    preparing: "Entering plan mode",
  },
  plan_read: {
    icon: "note",
    label: "Read plan",
    preparing: "Opening the plan",
  },
  plan_submit: {
    icon: "note",
    label: "Submit plan",
    preparing: "Writing up the plan",
  },

  platform_read: {
    icon: "library",
    label: "Read",
    keys: ["ref"],
    preparing: "Choosing a platform item to read",
  },
  platform_search: {
    icon: "search",
    label: "Search platform",
    keys: ["query"],
    noun: ["hit", "hits"],
    preparing: "Composing a platform search",
  },

  project_active: {
    icon: "layers",
    label: "Active project",
    preparing: "Checking the active project",
  },
  project_list: {
    icon: "layers",
    label: "Projects",
    noun: ["project", "projects"],
    preparing: "Listing the projects",
  },
  project_open_worktree: {
    icon: "branch",
    label: "Opened a worktree",
    preparing: "Naming a new worktree",
  },
  repo_inventory_agent_context: {
    icon: "branch",
    label: "Repo context",
    preparing: "Asking for repo context",
  },

  // A harness tool, like `search_tools` below, so it declares no category: a Python
  // script whose own tool calls are rows of their own, nested on this one's card. No
  // detail key — a script has no one argument that says what it is about, so the
  // narration leads and the open card shows the code.
  run_code: {
    icon: "code",
    label: "Script",
    keys: [],
    script: "code",
    preparing: "Writing a script",
  },

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
    preparing: "Asking for more tools",
  },

  shell_check_command: {
    icon: "terminal",
    label: "Check process",
    preparing: "Checking on a process",
  },
  shell_run_command: {
    icon: "terminal",
    label: "Shell",
    keys: ["command"],
    terminal: true,
    preparing: "Composing a shell command",
  },
  // Deliberately not a terminal: a background process is a handle the agent checks on
  // later, not a command whose output the operator watches arrive. Its result is that
  // handle, and a terminal showing one would be a terminal that never prints anything.
  shell_start_command: {
    icon: "terminal",
    label: "Start process",
    keys: ["command"],
    preparing: "Composing a background command",
  },
  shell_stop_command: {
    icon: "stop",
    label: "Stop process",
    preparing: "Stopping a process",
  },

  skills_create: {
    icon: "library",
    label: "New skill",
    keys: ["name"],
    preparing: "Writing a new skill",
  },
  skills_edit: {
    icon: "library",
    label: "Edit skill",
    keys: ["name", "explanation"],
    preparing: "Revising a skill",
  },
  skills_open: {
    icon: "library",
    label: "Open skill",
    keys: ["name"],
    preparing: "Picking a skill to open",
  },

  vault_get_entry: {
    icon: "key",
    label: "Secret",
    keys: ["entry_id"],
    preparing: "Asking for a secret",
  },
  vault_list_entries: {
    icon: "lock",
    label: "Vault",
    keys: ["reason"],
    noun: ["entry", "entries"],
    preparing: "Asking to list the vault",
  },

  view_close: {
    icon: "close",
    label: "Close view",
    preparing: "Closing the view",
  },
  view_show: {
    icon: "panel-right",
    label: "Show view",
    keys: ["title", "file", "path"],
    preparing: "Setting up a view",
  },

  web_fetch: {
    icon: "link",
    label: "Fetch",
    keys: ["url"],
    preparing: "Choosing a page to fetch",
  },
  web_search: {
    icon: "search",
    label: "Web search",
    keys: ["query"],
    noun: ["result", "results"],
    preparing: "Composing a web search",
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
  const bare = Object.hasOwn(CATEGORY_ICONS, category)
    ? name.slice(category.length + 1)
    : name;
  // The full registry name is the fallback, not the stripped remainder: `files_` has
  // nothing left after the prefix, and a blank row says less than the raw name does.
  return sentenceCase(bare, name);
}

/** The full row for a tool — presentation plus the summarizer's hints.
 *
 *  Own keys only: the name is whatever the model called, and a plain index would
 *  hand `constructor` or `toString_x` an `Object.prototype` member as its entry. */
export function toolEntry(name: string): ToolEntry {
  if (Object.hasOwn(TOOLS, name)) return TOOLS[name];
  const category = categoryOf(name);
  return {
    icon: Object.hasOwn(CATEGORY_ICONS, category)
      ? CATEGORY_ICONS[category]
      : DEFAULT_ICON,
    label: humanize(name),
  };
}

/** What the row says while the call's arguments are still being written: the tool's
 *  own phrase, else its category's, else its label. */
export function preparingPhrase(name: string): string {
  const own = toolEntry(name).preparing;
  if (own) return own;
  const category = categoryOf(name);
  if (Object.hasOwn(CATEGORY_PREPARING, category))
    return CATEGORY_PREPARING[category];
  return `Preparing ${toolEntry(name).label.toLowerCase()}`;
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
