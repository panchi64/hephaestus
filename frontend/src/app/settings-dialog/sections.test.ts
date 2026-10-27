import { describe, expect, test } from "bun:test";
import {
  SETTINGS_GROUPS,
  allSectionIds,
  allSections,
  groupForSection,
  sectionById,
  sectionHeading,
} from "./sections";
import { searchSettingsSections } from "./sections-search";

describe("the section model", () => {
  test("every section is addressable and describable", () => {
    for (const section of allSections()) {
      // The id is a URL slug, so it has to survive being typed and bookmarked.
      expect(section.id).toMatch(/^[a-z][a-z-]*$/);
      expect(section.label.length).toBeGreaterThan(0);
      expect(section.description.length).toBeGreaterThan(0);
    }
  });

  test("ids are unique across every group", () => {
    // The id is what a deep link and a renderer key both name, so the same slug
    // under two groups would be two panes answering to one key.
    const ids = allSectionIds();
    expect(new Set(ids).size).toBe(ids.length);
    const groups = SETTINGS_GROUPS.map((g) => g.id);
    expect(new Set(groups).size).toBe(groups.length);
  });

  test("a group is a heading, never empty", () => {
    for (const group of SETTINGS_GROUPS) {
      expect(group.label.length).toBeGreaterThan(0);
      expect(group.sections.length).toBeGreaterThan(0);
    }
  });

  test("every section knows the group it sits under", () => {
    for (const section of allSections())
      expect(groupForSection(section.id)?.sections).toContain(section);
    expect(groupForSection("no-such-section")).toBeUndefined();
  });

  test("an unknown or missing id lands on the first section", () => {
    // A hand-typed slug opens the dialog on something rather than on an empty
    // pane — the failure mode this fallback exists to prevent.
    expect(sectionById(undefined).id).toBe(allSections()[0].id);
    expect(sectionById("no-such-section").id).toBe(allSections()[0].id);
    expect(sectionById("models").id).toBe("models");
  });

  test("a heading is the nav row's label and description, and never falls back", () => {
    const models = sectionById("models");
    expect(sectionHeading("models")).toEqual({
      label: models.label,
      description: models.description,
    });
    expect(sectionHeading("no-such-section")).toBeUndefined();
  });
});

describe("searchSettingsSections", () => {
  test("an empty query matches nothing", () => {
    // Empty means "not searching" — the column shows its groups instead, so
    // returning everything here would render the whole list twice.
    expect(searchSettingsSections("")).toEqual([]);
    expect(searchSettingsSections("   ")).toEqual([]);
  });

  test("a label match outranks a keyword or description match", () => {
    // The ordering is the reason the field beats the accordion: typing "model"
    // must put MODELS first, not behind sections that merely mention one.
    const hits = searchSettingsSections("model");
    expect(hits[0].section.id).toBe("models");
    // MCP matches on the keyword "model context protocol", so it is in the
    // result — behind the section actually named Models.
    expect(hits.map((h) => h.section.id)).toContain("mcp");
    expect(hits.findIndex((h) => h.section.id === "mcp")).toBeGreaterThan(0);
  });

  test("a section appears once, in its strongest bucket only", () => {
    const hits = searchSettingsSections("memory");
    const ids = hits.map((h) => h.section.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  test("keywords find a pane that never says the word", () => {
    // "endpoint" appears in no section label. Without the keyword list, an
    // operator looking for one would search the column and find nothing.
    expect(
      searchSettingsSections("endpoint").map((h) => h.section.id),
    ).toContain("models");
    expect(searchSettingsSections("theme").map((h) => h.section.id)).toContain(
      "appearance",
    );
    expect(
      searchSettingsSections("searxng").map((h) => h.section.id),
    ).toContain("web-search");
  });

  test("matching is case- and whitespace-insensitive", () => {
    expect(searchSettingsSections("  MCP  ").map((h) => h.section.id)).toEqual(
      searchSettingsSections("mcp").map((h) => h.section.id),
    );
  });

  test("the group name alone is not a match", () => {
    // Typing "system" must not return BACKUP and HEALTH, neither of which the
    // operator named — the group is sorting, not a search target.
    const ids = searchSettingsSections("system").map((h) => h.section.id);
    expect(ids).not.toContain("backup");
    expect(ids).not.toContain("health");
  });

  test("every hit carries the group it belongs to", () => {
    // A flat result list drops the headings, so the row has to say where it is.
    for (const hit of searchSettingsSections("a"))
      expect(hit.group.sections).toContain(hit.section);
  });
});
