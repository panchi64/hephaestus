import { describe, expect, test } from "bun:test";
import {
  TOOLS,
  preparingPhrase,
  toolEntry,
  toolPresentation,
  toolRowLabel,
} from "./toolPresentation";
import type { ToolInvocation } from "./model";

function tool(part: Partial<ToolInvocation>): ToolInvocation {
  return {
    id: "t1",
    name: "files_read_file",
    args: "",
    status: "ok",
    ...part,
  };
}

describe("a tool reads as a glyph and a short label", () => {
  test("a table entry wins", () => {
    expect(toolPresentation("files_read_file")).toEqual({
      icon: "file",
      label: "Read",
    });
    expect(toolPresentation("web_search")).toEqual({
      icon: "search",
      label: "Web search",
    });
  });

  // The table is a convenience, not a gate: a tool that lands in the backend
  // before it lands here still renders in the right family.
  test("an unlisted tool falls back to its category's glyph and its own name", () => {
    expect(toolPresentation("calendar_move_event")).toEqual({
      icon: "calendar",
      label: "Move event",
    });
  });

  // Connector tools are `external_<slug>_<action>` and are discovered per
  // operator, so no table could enumerate them.
  test("a connector action keeps its connector in the label", () => {
    expect(toolPresentation("external_linear_create_issue")).toEqual({
      icon: "plug",
      label: "Linear create issue",
    });
  });

  // Dropping the first word of a name whose prefix ISN'T a category would lose
  // meaning, so an unrecognized name is humanized whole.
  test("an unrecognized prefix is kept rather than guessed away", () => {
    expect(toolPresentation("wildcat_probe")).toEqual({
      icon: "plug",
      label: "Wildcat probe",
    });
    expect(toolPresentation("standalone")).toEqual({
      icon: "plug",
      label: "Standalone",
    });
  });

  // The harness's own tool, offered so a model carrying only an index of the tool
  // groups it doesn't hold can ask for one. `search` is not a namespace, so the row
  // has to be listed explicitly or it inherits nothing and reads as bookkeeping in
  // the middle of the work.
  test("loading a dormant group reads as work in progress, in no family", () => {
    expect(toolPresentation("search_tools")).toEqual({
      icon: "grid",
      label: "Loading tools",
    });
    expect(
      toolRowLabel(tool({ name: "search_tools", detail: "browse, mail" })),
    ).toBe("Loading tools · browse, mail");
  });

  // A name with no underscore declares no namespace. Reading one out of it
  // anyway would give `views` the `view` category's glyph and strip its own
  // label away to nothing.
  test("a name that declares no namespace is not given one", () => {
    expect(toolPresentation("views")).toEqual({
      icon: "plug",
      label: "Views",
    });
    expect(toolPresentation("webs")).toEqual({ icon: "plug", label: "Webs" });
  });

  // The name is whatever the model called. An `Object.prototype` member is not an
  // entry: indexing one out handed `Icon` an undefined name, which threw.
  test("a name that collides with an object built-in falls back like any other", () => {
    expect(toolPresentation("constructor")).toEqual({
      icon: "plug",
      label: "Constructor",
    });
    expect(toolPresentation("toString_thing")).toEqual({
      icon: "plug",
      label: "ToString thing",
    });
  });
});

describe("a call's work-log outline says what the call does", () => {
  test("a read that brings context into the model is a diamond", () => {
    for (const name of [
      "skills_open",
      "memory_recall",
      "memory_open",
      "memory_list",
      "memory_audit",
      "repo_inventory_agent_context",
      "search_tools",
      "plan_read",
      "tasks_read",
      "corpus_retrieve",
    ])
      expect([name, toolEntry(name).frame]).toEqual([name, "diamond"]);
  });

  test("a brief or message to a sub-agent points out", () => {
    expect(toolEntry("agents_spawn").frame).toBe("out");
    expect(toolEntry("agents_message").frame).toBe("out");
  });

  // Split by effect, not by family: writing a memory or a skill builds something.
  // No frame is the socket's default square.
  test("everything else is a square, writes in a context family included", () => {
    for (const name of [
      "memory_remember",
      "skills_create",
      "agents_stop",
      "memory_unlisted",
      "external_linear_create_issue",
    ])
      expect([name, toolEntry(name).frame]).toEqual([name, undefined]);
  });
});

describe("one-line form, for places with no room for a card", () => {
  test("the salient detail joins the label", () => {
    expect(toolRowLabel(tool({ detail: "backend/app.py" }))).toBe(
      "Read · backend/app.py",
    );
  });

  test("the full argument summary stands in when nothing stood out", () => {
    expect(toolRowLabel(tool({ args: "limit=5" }))).toBe("Read · limit=5");
  });

  test("a tool with no arguments at all is just its label", () => {
    expect(toolRowLabel(tool({ name: "builtin_now" }))).toBe("Clock");
  });
});

describe("the phrase shown while a call's arguments are being written", () => {
  test("every table entry has its own", () => {
    for (const [name, entry] of Object.entries(TOOLS))
      expect([name, typeof entry.preparing]).toEqual([name, "string"]);
  });

  test("the browser tools are in the table", () => {
    expect(toolPresentation("browse_navigate").label).toBe("Navigate");
    expect(toolPresentation("browse_unlisted").icon).toBe("eye");
  });

  test("an unlisted tool falls back to its category", () => {
    expect(preparingPhrase("external_linear_create_issue")).toBe(
      "Preparing a connector call",
    );
    expect(preparingPhrase("browse_unlisted")).toBe(
      "Preparing a browser action",
    );
  });

  test("a tool in no category falls back to its label", () => {
    expect(preparingPhrase("widget")).toBe("Preparing widget");
  });
});

describe("platform search and read", () => {
  test("search leads with its query and counts hits", () => {
    expect(toolPresentation("platform_search")).toEqual({
      icon: "search",
      label: "Search platform",
    });
    expect(toolPresentation("platform_read")).toEqual({
      icon: "library",
      label: "Read",
    });
  });
});
