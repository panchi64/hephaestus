import {
  settingsGroups,
  type SettingsGroup,
  type SettingsSection,
} from "./sections";

/** One search result: the section, and the group it sits under — a flat result
 *  list drops the headings, so each row has to carry its own place. */
export interface SettingsSectionHit {
  section: SettingsSection;
  group: SettingsGroup;
}

/**
 * The dialog's left-column search.
 *
 * **Label ▸ keyword ▸ description, and the buckets are exclusive** — a section
 * matched on its label ranks above one matched only on a word in its blurb, and
 * appears once. That ordering is the whole reason the field beats the accordion
 * for finding things: typing "model" should put MODELS first, not fourth behind
 * three sections whose descriptions happen to mention a model.
 *
 * Pure, and it imports only the section data — so it is unit-testable without a
 * DOM and without constructing any feature's resources.
 */
export function searchSettingsSections(query: string): SettingsSectionHit[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];

  const byLabel: SettingsSectionHit[] = [];
  const byKeyword: SettingsSectionHit[] = [];
  const byDescription: SettingsSectionHit[] = [];

  for (const group of settingsGroups()) {
    for (const section of group.sections) {
      const hit = { section, group };
      if (section.label.toLowerCase().includes(q)) byLabel.push(hit);
      else if (section.keywords?.some((k) => k.toLowerCase().includes(q)))
        byKeyword.push(hit);
      else if (section.description.toLowerCase().includes(q))
        byDescription.push(hit);
      // The group's own name is deliberately not matched: "system" would
      // otherwise return BACKUP and HEALTH, neither of which the operator typed.
    }
  }

  return [...byLabel, ...byKeyword, ...byDescription];
}
