import { createMemo, createSignal, Show, type JSX } from "solid-js";
import { Icon, Menu, Tooltip, toast } from "~/ui";
import { settled } from "~/lib/resource";
import {
  activeProjectId,
  isUnscoped,
  setActiveProject,
  setUnscoped,
  useProjects,
} from "~/lib/stores/projects";
import { useSettingsRoute } from "~/app/settings-dialog";

/** Sentinels. Real project ids are hex, so neither can collide with one. */
const ALL = "__all__";
const NONE = "__none__";
const MANAGE = "__manage__";

/** The project scope, as a dim filter at the right end of the rail's list heading
 *  (`RECENTS · 24   All projects ▾`). It is mounted on every route with the rail, so
 *  the scope is readable and changeable from every surface it affects.
 *
 *  It renders nothing until the operator has at least one project. A control that
 *  scopes nothing is just a row of vertical space explaining a feature they haven't
 *  used, and the app before projects is exactly the app with none. */
export function ProjectSwitcher(): JSX.Element {
  const projects = useProjects();
  const settings = useSettingsRoute();

  const options = createMemo(() => {
    const rows = (settled(projects)?.projects ?? []).filter((p) => !p.archived);
    return [
      { value: NONE, label: "Unfiled only" },
      { value: ALL, label: "All projects" },
      ...rows.map((p) => ({ value: p.id, label: p.name })),
      { value: MANAGE, label: "Manage projects…" },
    ];
  });

  /** The option the operator just picked, while its activation is still in the air.
   *
   *  The control is otherwise driven entirely by `activeProjectId()`, which only
   *  moves once the backend has answered — so the switcher spent the whole round trip
   *  showing the project the operator had just left, which reads as a click that
   *  didn't take and invites the second pick that caused the race underneath. Holding
   *  the pick here lets the trigger say what they chose immediately; the store stays
   *  the authority on what is actually active. */
  const [picking, setPicking] = createSignal<string | null>(null);

  const value = (): string =>
    picking() ?? (isUnscoped() ? ALL : (activeProjectId() ?? NONE));

  const onChange = (next: string): void => {
    if (next === MANAGE) {
      // Projects is a section of the settings dialog now, not a page. The
      // `/projects` route still forwards there, but going through it would push a
      // navigation the operator never asked for — this opens the dialog where
      // they stand.
      settings.show("projects");
      return;
    }
    if (next === ALL) {
      // Client-side and instant, so it owns the trigger immediately — including over
      // an activation still in the air, whose answer `adopt` will honour as unscoped
      // when it lands.
      setPicking(null);
      setUnscoped(true);
      return;
    }
    setUnscoped(false);
    setPicking(next);
    void setActiveProject(next === NONE ? null : next)
      .catch((err: unknown) => {
        toast.error(
          err instanceof Error ? err.message : "Could not switch project",
        );
      })
      .finally(() => {
        // Only the pick still on screen clears the pending state. An earlier
        // activation settling after a later one would otherwise drop the switcher
        // back to the backend's echo mid-flight — the same flicker, one step later.
        if (picking() === next) setPicking(null);
      });
  };

  const current = () =>
    options().find((o) => o.value === value())?.label ?? "All projects";
  const label = () => (picking() ? `${current()} · switching…` : current());

  // A dim trigger at the right end of the list's heading, not a field of its own:
  // the scope is a quiet qualifier on the list, and a full select above the threads
  // spent a row on it. At the rail's narrowest it is a funnel, its name in the
  // tooltip. The control stays live throughout a switch — a second pick supersedes
  // the first cleanly, and a switcher disabled on a hung request would be dead with
  // no way back.
  return (
    <Show when={(settled(projects)?.projects ?? []).length > 0}>
      <Tooltip label={`Project filter: ${label()}`} side="bottom" delay={600}>
        <Menu
          align="right"
          items={options().map((o) => ({
            label: o.value === value() ? `${o.label} ✓` : o.label,
            separated: o.value === MANAGE,
            onSelect: () => onChange(o.value),
          }))}
          trigger={
            <span
              aria-label={`Project filter: ${label()}`}
              class="flex min-w-0 items-center gap-1 font-mono text-meta text-dim hover:text-text"
            >
              <Icon name="filter" size={16} class="ody-rail-narrow" />
              <span class="ody-rail-wide min-w-0 truncate">{label()}</span>
              <Icon name="chevron-down" size={12} class="ody-rail-wide" />
            </span>
          }
        />
      </Tooltip>
    </Show>
  );
}
