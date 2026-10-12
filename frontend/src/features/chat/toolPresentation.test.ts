import { describe, expect, test } from "bun:test";
import { toolPresentation, toolRowLabel } from "./toolPresentation";
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
