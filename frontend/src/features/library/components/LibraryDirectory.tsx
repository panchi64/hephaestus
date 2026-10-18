import { For, Index, Show, Suspense, type JSX } from "solid-js";
import { createListView, type SortDef, type SortDir } from "~/lib/list";
import { useTabParam } from "~/lib/useTabParam";
import {
  Button,
  EmptyState,
  HIDDEN_FILE_INPUT,
  InfoHint,
  InstrumentBand,
  ListRow,
  ListToolbar,
  LoadingText,
  Menu,
  PageHeader,
  Panel,
  Row,
  Stack,
  Tabs,
  Text,
  useFileDrop,
  type BandCell,
  type IconName,
  type MenuItem,
} from "~/ui";
import {
  filterByTab,
  LIBRARY_TABS,
  LIBRARY_TAB_VALUES,
  searchText,
  type LibraryEntry,
  type LibraryGroup,
  type LibraryTab,
} from "../model";

export interface LibraryDirectoryProps<T extends LibraryEntry> {
  title: string;
  subtitle: string;
  assetId: string;
  /** Singular and plural nouns for the empty states and the New button. */
  noun: string;
  nouns: string;
  icon: IconName;
  items: T[];
  loadError: string | null;
  sorts: Record<string, SortDef<T>>;
  initialSort: string;
  initialDir: SortDir;
  /** Cells after the shared Total / Published / Draft. */
  bandExtra?: BandCell[];
  /** Text of the two info hints beside the tabs. */
  draftHint: string;
  importHint: string;
  importAccept: string;
  importing: boolean;
  onImport: (file: File | undefined) => void;
  /** Absent where items are not created by hand; the New button is omitted. */
  onNew?: () => void;
  onOpen: (item: T) => void;
  /** Everything on the right of a row except the `…` menu. */
  rowRight: (item: T) => JSX.Element;
  menuItems: (item: T) => MenuItem[];
  /** Splits the searched, sorted rows into headed sections; without it the list is flat. */
  groups?: (visible: T[]) => LibraryGroup<T>[];
  /** Hint shown under an empty unfiltered list. */
  emptyCreateHint: string;
  /** The create dialog, rendered once beside the list. */
  children?: JSX.Element;
}

/** The directory both Skills and Agents render: header with Import / New,
 *  instrument band, All / Published / Draft tabs, search + sort, and a row list,
 *  flat or grouped. What differs between them arrives as props and render slots. */
export function LibraryDirectory<T extends LibraryEntry>(
  props: LibraryDirectoryProps<T>,
): JSX.Element {
  const [tab, setTab] = useTabParam<LibraryTab>(
    "tab",
    LIBRARY_TAB_VALUES,
    "all",
  );
  const picker = useFileDrop((files) => props.onImport(files[0]));

  const publishedCount = () => props.items.filter((i) => i.published).length;
  const draftCount = () => props.items.length - publishedCount();

  const view = createListView({
    source: () => filterByTab(props.items, tab()),
    search: searchText,
    sorts: props.sorts,
    initialSort: props.initialSort,
    initialDir: props.initialDir,
  });

  const row = (item: T) => (
    <ListRow
      label={item.name}
      leading={props.icon}
      onClick={() => props.onOpen(item)}
      right={
        <span class="flex shrink-0 items-center gap-3">
          {props.rowRight(item)}
          {/* Stop row navigation when interacting with the menu. */}
          <span
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
            }}
          >
            <Menu
              trigger={
                <span class="px-1 text-dim hover:text-bright">
                  <Text variant="micro">···</Text>
                </span>
              }
              items={props.menuItems(item)}
            />
          </span>
        </span>
      }
    />
  );

  const emptyHint = (): string => {
    if (view.isFiltered()) return `No ${props.nouns} match your search.`;
    return tab() === "all"
      ? props.emptyCreateHint
      : `No ${props.nouns} match the current filter.`;
  };

  return (
    <Stack gap={6}>
      <PageHeader
        variant="section"
        title={props.title}
        subtitle={props.subtitle}
        assetId={props.assetId}
        actions={
          <Row gap={2}>
            <input
              ref={picker.bindInput}
              {...HIDDEN_FILE_INPUT}
              multiple={false}
              accept={props.importAccept}
              {...picker.inputHandlers}
            />
            <Button
              variant="default"
              leading="upload"
              disabled={props.importing}
              onClick={picker.openPicker}
            >
              {props.importing ? "Importing…" : "Import"}
            </Button>
            <Show when={props.onNew}>
              {(onNew) => (
                <Button variant="primary" leading="plus" onClick={onNew()}>
                  New {props.noun}
                </Button>
              )}
            </Show>
          </Row>
        }
      />

      <InstrumentBand
        items={[
          { label: "Total", value: String(props.items.length) },
          {
            label: "Published",
            value: String(publishedCount()),
            tone: "nominal",
          },
          { label: "Draft", value: String(draftCount()), tone: "dim" },
          ...(props.bandExtra ?? []),
        ]}
      />

      <Panel flush>
        <div class="flex items-center justify-between gap-3 pr-3">
          <Tabs
            items={[...LIBRARY_TABS]}
            value={tab()}
            onChange={(v) => setTab(v as LibraryTab)}
          />
          <Row align="center" gap={3}>
            <Row align="center" gap={1}>
              <Text variant="micro" tone="dim">
                Draft
              </Text>
              <InfoHint label={props.draftHint} />
            </Row>
            <Row align="center" gap={1}>
              <Text variant="micro" tone="dim">
                Import
              </Text>
              <InfoHint label={props.importHint} />
            </Row>
          </Row>
        </div>

        <div class="p-3">
          <ListToolbar
            query={view.query()}
            onQueryChange={view.setQuery}
            placeholder="Search by name or description…"
            sortKey={view.sortKey()}
            sortOptions={view.sortOptions}
            onSortChange={view.setSort}
            dir={view.dir()}
            onToggleDir={view.toggleDir}
            count={view.count()}
            total={view.total()}
          />
        </div>

        <Suspense
          fallback={
            <div class="p-4">
              <LoadingText />
            </div>
          }
        >
          <Show
            when={view.items().length}
            fallback={
              <EmptyState
                icon={props.icon}
                message={
                  props.loadError
                    ? `${capitalize(props.nouns)} unavailable`
                    : `No ${props.nouns}`
                }
                hint={props.loadError ?? emptyHint()}
              />
            }
          >
            <Show
              when={props.groups}
              fallback={<For each={view.items()}>{row}</For>}
            >
              {/* `Index`: the grouping builds fresh group objects on every change, so
                  keying sections by reference would remount every row with them. */}
              {(groups) => (
                <Index each={groups()(view.items())}>
                  {(group) => (
                    <section>
                      <div class="flex items-baseline gap-3 px-3 pt-4 pb-1">
                        <Text variant="plate">{group().label}</Text>
                        <Text variant="micro" tone="dim" class="truncate">
                          {group().note}
                        </Text>
                      </div>
                      <For each={group().items}>{row}</For>
                    </section>
                  )}
                </Index>
              )}
            </Show>
          </Show>
        </Suspense>
      </Panel>

      {props.children}
    </Stack>
  );
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
