import { describe, expect, test } from "bun:test";
import { AREAS } from "~/app/nav";
import { envelopeLabel } from "~/features/chat/envelopeLabels";
import { toolEntry } from "~/features/chat/toolPresentation";
import { BUILTIN_PLUGINS, pluginOfHref } from "./registry";

describe("built-in plugin UI", () => {
  test("a plugin's tool rows and category glyph reach the transcript", () => {
    expect(toolEntry("mail_send").label).toBe("Send mail");
    expect(toolEntry("routines_write").icon).toBe("clock");
  });

  test("a plugin's pages join COMMS and are traced back to it", () => {
    const hrefs = AREAS[0].items.map((item) => item.href);
    expect(hrefs).toContain("/email");
    expect(hrefs).toContain("/tasks");
    expect(pluginOfHref("/email")).toBe("mail");
    expect(pluginOfHref("/calendar")).toBeUndefined();
  });

  test("a plugin labels the untrusted parts its turns carry", () => {
    expect(
      envelopeLabel(
        { kind: "untrusted", source: "the routine's previous run", text: "" },
        "message",
      ).label,
    ).toBe("Previous run");
  });

  test("every plugin is named once", () => {
    const names = BUILTIN_PLUGINS.map((plugin) => plugin.name);
    expect(new Set(names).size).toBe(names.length);
  });
});
