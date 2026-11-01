import { createSignal, For, Show, type JSX } from "solid-js";
import {
  Button,
  confirm,
  EmptyState,
  Input,
  ListRow,
  PathInput,
  Resource,
  Row,
  Stack,
  Text,
  toast,
} from "~/ui";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import { usePathPicker } from "~/lib/hostPicker";
import {
  activeProjectId,
  createProject,
  deleteProject,
  setActiveProject,
  useProjects,
} from "~/lib/stores/projects";
import { RepoStatus } from "../components/RepoStatus";

/** The projects library: add a directory, switch between them, retire one.
 *
 *  Everything here relays — the backend owns the selection, probes the git state,
 *  and validates the path. What the screen adds is the one thing worth saying out
 *  loud beside each row: whether the directory is a repo, and how much uncommitted
 *  work the agent will not be able to see (see `RepoStatus`). */
export function ProjectsScreen(): JSX.Element {
  const projects = useProjects();
  const picker = usePathPicker();

  const [name, setName] = createSignal("");
  const [path, setPath] = createSignal("");
  const [adding, setAdding] = createSignal(false);

  const add = async (): Promise<void> => {
    if (!path().trim()) {
      toast.error("Pick a directory first");
      return;
    }
    setAdding(true);
    try {
      const created = await createProject(name().trim(), path().trim());
      setName("");
      setPath("");
      toast.success(`Added ${created.name}`);
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Could not add the project",
      );
    } finally {
      setAdding(false);
    }
  };

  const remove = async (id: string, label: string): Promise<void> => {
    const ok = await confirm({
      title: `Remove ${label}?`,
      detail:
        "This only removes the project. The directory and its files are left exactly as they are, and anything filed under it — conversations, tasks, indexed folders — becomes unfiled rather than being deleted.",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteProject(id);
      toast.success(`Removed ${label}`);
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Could not remove the project",
      );
    }
  };

  const activate = (id: string): void => {
    void setActiveProject(id).catch((err: unknown) => {
      toast.error(
        err instanceof Error ? err.message : "Could not switch project",
      );
    });
  };

  return (
    <Stack gap={6}>
      <SettingsPageHeader section="projects" />

      <Stack gap={3}>
        <Text variant="label" tone="dim">
          Add a project
        </Text>
        <PathInput
          label="Directory"
          value={path()}
          onChange={setPath}
          onBrowse={
            picker()
              ? () =>
                  picker()!({
                    mode: "directory",
                    title: "Choose a project folder",
                  })
              : undefined
          }
          hint="An absolute path on this machine."
        />
        <Input
          label="Name"
          value={name()}
          onChange={setName}
          placeholder="Defaults to the folder name"
        />
        <Row gap={2}>
          <Button
            variant="primary"
            onClick={() => void add()}
            disabled={adding()}
          >
            {adding() ? "Adding…" : "Add project"}
          </Button>
        </Row>
      </Stack>

      <Resource
        data={projects}
        isEmpty={(d) => d.projects.length === 0}
        empty={
          <EmptyState
            message="No projects"
            hint="Add a directory above to scope your work by project."
          />
        }
      >
        {(data) => (
          <Stack gap={2}>
            <Text variant="label" tone="dim">
              Your projects
            </Text>
            <For each={data().projects}>
              {(project) => (
                <ListRow
                  label={project.name}
                  description={project.rootPath}
                  selected={project.id === activeProjectId()}
                  right={
                    <Row gap={3} class="items-center">
                      <RepoStatus repo={project.repo} />
                      <Show when={project.id !== activeProjectId()}>
                        <Button size="sm" onClick={() => activate(project.id)}>
                          Open
                        </Button>
                      </Show>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => void remove(project.id, project.name)}
                      >
                        Remove
                      </Button>
                    </Row>
                  }
                />
              )}
            </For>
          </Stack>
        )}
      </Resource>
    </Stack>
  );
}
