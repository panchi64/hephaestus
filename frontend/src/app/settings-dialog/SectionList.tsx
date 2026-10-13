import { createEffect, createSignal, For, on, Show, type JSX } from "solid-js";
import { Disclosure, Icon, Input, ListRow, Text, Tooltip, cx } from "~/ui";
import { readLS, writeLS } from "~/lib/storage";
import { SETTINGS_GROUPS, type SettingsSection } from "./sections";
import { searchSettingsSections } from "./sections-search";

/** Which groups the operator has folded away, at module scope so the column
 *  looks the way they left it the next time the dialog opens. Not persisted:
 *  it is a view state, not a preference, and the backend owns preferences. */
const [collapsed, setCollapsed] = createSignal<ReadonlySet<string>>(new Set());

const NAV_COLLAPSED_KEY = "heph.settings.navCollapsed";

/** Whether the column is folded to its icon rail. Remembered across opens, and
 *  across reloads where storage allows; unreadable storage reads as expanded. */
const [navCollapsed, setNavCollapsed] = createSignal(
  readLS(NAV_COLLAPSED_KEY) === "1",
);

function toggleNav(): void {
  const next = !navCollapsed();
  setNavCollapsed(next);
  writeLS(NAV_COLLAPSED_KEY, next ? "1" : "0");
}

function NavToggle(): JSX.Element {
  return (
    <button
      type="button"
      onClick={toggleNav}
      aria-label={
        navCollapsed()
          ? "Expand settings navigation"
          : "Collapse settings navigation"
      }
      aria-expanded={!navCollapsed()}
      title={navCollapsed() ? "Expand navigation" : "Collapse navigation"}
      class="flex size-8 shrink-0 items-center justify-center text-dim transition-colors hover:bg-raised hover:text-bright"
    >
      <Icon name="panel-right" size={16} class="-scale-x-100" />
    </button>
  );
}

function toggleGroup(id: string): void {
  setCollapsed((prev) => {
    const next = new Set(prev);
    if (!next.delete(id)) next.add(id);
    return next;
  });
}

/**
 * The dialog's left column: a search field over every configuration surface,
 * and beneath it the surfaces themselves, grouped under collapsible headings.
 *
 * **Rows are surfaces, not categories.** The column used to list six umbrellas
 * and hide sixteen panes inside them, which meant knowing that "offline mode"
 * was filed under GENERAL before you could find it. Now the thing you came to
 * change is the thing you click, the headings only sort them, and the field at
 * the top skips the sorting entirely — type three letters and the hierarchy
 * stops mattering.
 *
 * A search result list is **flat and ranked** (label ▸ keyword ▸ description)
 * rather than grouped: once the operator has told us what they want, group
 * headers are furniture between them and it, so each row carries its group as
 * dim meta instead.
 *
 * Selection is the same raised fill the rail uses (§10 States), not a rule down
 * the side: this column already sits beside the pane, and a second vertical line
 * a few pixels from that edge reads as a mistake.
 */
export function SectionList(props: {
  active: SettingsSection;
  onSelect: (id: string) => void;
}): JSX.Element {
  const [query, setQuery] = createSignal("");
  const hits = () => searchSettingsSections(query());
  const searching = () => query().trim() !== "";

  // A deep link can land on a section inside a group the
  // operator folded away — the row has to be visible where it says it is, so the
  // group unfolds rather than the selection hiding inside it.
  //
  // `on` the active id ALONE, and the difference is load-bearing: a plain effect
  // would also depend on `collapsed`, so folding away the group you are standing
  // in would immediately unfold it again and the header would look broken. This
  // reacts to arriving somewhere, not to the fold state changing under it.
  createEffect(
    on(
      () => props.active.id,
      (id) => {
        const group = SETTINGS_GROUPS.find((g) =>
          g.sections.some((s) => s.id === id),
        );
        if (group && collapsed().has(group.id)) toggleGroup(group.id);
      },
    ),
  );

  const row = (section: SettingsSection, meta?: string) => (
    <Tooltip
      delay={1000}
      side="right"
      label={section.description}
      class="block w-full"
    >
      <ListRow
        label={section.label}
        leading={section.icon}
        selected={section.id === props.active.id}
        right={
          meta ? (
            <Text variant="micro" tone="dim">
              {meta}
            </Text>
          ) : undefined
        }
        onClick={() => props.onSelect(section.id)}
      />
    </Tooltip>
  );

  return (
    <nav
      class={cx(
        "scrollbar-thin flex shrink-0 flex-col gap-2 overflow-y-auto p-2",
        navCollapsed() ? "w-12 items-center" : "w-56",
      )}
      aria-label="Settings sections"
    >
      <Show when={navCollapsed()}>
        <NavToggle />
        <Tooltip label="Search settings" side="right" class="block">
          <button
            type="button"
            aria-label="Search settings"
            onClick={toggleNav}
            class="flex size-8 items-center justify-center text-dim transition-colors hover:bg-raised hover:text-bright"
          >
            <Icon name="search" size={16} />
          </button>
        </Tooltip>
        <For each={SETTINGS_GROUPS}>
          {(group) => (
            <div class="flex flex-col gap-0.5 border-t border-line pt-2">
              <For each={group.sections}>
                {(section) => (
                  <Tooltip label={section.label} side="right" class="block">
                    <button
                      type="button"
                      aria-label={section.label}
                      aria-current={
                        section.id === props.active.id ? "page" : undefined
                      }
                      onClick={() => props.onSelect(section.id)}
                      class={cx(
                        "flex size-8 items-center justify-center transition-colors hover:bg-raised hover:text-bright",
                        section.id === props.active.id
                          ? "bg-raised text-bright"
                          : "text-dim",
                      )}
                    >
                      <Icon name={section.icon} size={16} />
                    </button>
                  </Tooltip>
                )}
              </For>
            </div>
          )}
        </For>
      </Show>
      <Show when={!navCollapsed()}>
        <div class="flex items-center gap-1 px-1 pt-1">
          <div class="min-w-0 flex-1">
            <Input
              leading="search"
              placeholder="Search settings…"
              aria-label="Search settings"
              value={query()}
              onInput={(e) => setQuery(e.currentTarget.value)}
              onKeyDown={(e) => {
                // Enter takes the top hit — the field is the fastest path to a pane,
                // and making the operator leave the keyboard to click would undo that.
                if (e.key === "Enter") {
                  const first = hits()[0];
                  if (first) props.onSelect(first.section.id);
                  return;
                }
                // Escape clears the query before it closes the dialog: a typed
                // search is the thing on screen, so it is the thing Escape undoes.
                if (e.key === "Escape" && searching()) {
                  e.stopPropagation();
                  setQuery("");
                }
              }}
            />
          </div>
          <NavToggle />
        </div>

        <Show
          when={searching()}
          fallback={
            <div class="flex flex-col gap-2">
              <For each={SETTINGS_GROUPS}>
                {(group) => (
                  <Disclosure
                    label={group.label}
                    open={!collapsed().has(group.id)}
                    onToggle={() => toggleGroup(group.id)}
                    triggerClass="w-full px-2 py-1"
                    class="mt-0.5 flex flex-col gap-0.5"
                  >
                    <For each={group.sections}>{(section) => row(section)}</For>
                  </Disclosure>
                )}
              </For>
            </div>
          }
        >
          <Show
            when={hits().length}
            fallback={
              <div class="px-3 py-2">
                <Text variant="micro" tone="dim">
                  Nothing matches “{query().trim()}”.
                </Text>
              </div>
            }
          >
            <div class="flex flex-col gap-0.5">
              <For each={hits()}>
                {(hit) => row(hit.section, hit.group.label)}
              </For>
            </div>
          </Show>
        </Show>
      </Show>
    </nav>
  );
}
