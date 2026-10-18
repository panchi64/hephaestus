import { Show, Suspense, type JSX } from "solid-js";
import {
  StatusFlag,
  Chip,
  Button,
  EmptyState,
  LoadingText,
  confirm,
  toast,
} from "~/ui";
import {
  LibraryDetail,
  type LibraryFileSource,
  type LibraryProperty,
} from "~/features/library";
import {
  RevisionHistory,
  refreshRevisions,
  revisionsBase,
} from "~/features/revisions";
import { settled } from "~/lib/resource";
import { relativeTime } from "~/lib/format";
import {
  deleteSkill,
  deleteSkillFile,
  exportSkill,
  getSkillDocument,
  getSkillFileText,
  putSkillDocument,
  putSkillFile,
  setSkillPinned,
  setSkillPublished,
  skillErrorMessage,
  refreshSkillDetail,
  useSkillDetail,
} from "../data";
import {
  PIN_DRAFT_HINT,
  PIN_HINT,
  skillSourceFlag,
  skillStatusFlag,
  skillStatusLabel,
  type Skill,
} from "../model";

const MAIN_FILE = "SKILL.md";

function baseName(path: string): string {
  return path.split("/").pop() ?? path;
}

function filesOf(skill: Skill): LibraryFileSource[] {
  return [
    { path: MAIN_FILE, load: () => getSkillDocument(skill.id) },
    ...skill.files.map((f) => ({
      path: f.relpath,
      load: () => getSkillFileText(skill.id, f.relpath),
    })),
  ];
}

function propertiesOf(skill: Skill): LibraryProperty[] {
  const rows: LibraryProperty[] = [
    { label: "Source", value: skill.source.toUpperCase() },
  ];
  if (skill.license) rows.push({ label: "License", value: skill.license });
  if (skill.compatibility) {
    rows.push({ label: "Compatibility", value: skill.compatibility });
  }
  if (skill.allowedTools?.length) {
    rows.push({
      label: "Allowed tools",
      value: (
        <span class="flex flex-wrap gap-1">
          {skill.allowedTools.map((tool) => (
            <Chip>{tool}</Chip>
          ))}
        </span>
      ),
    });
  }
  for (const [key, value] of Object.entries(skill.extras ?? {})) {
    rows.push({
      label: key,
      value: typeof value === "string" ? value : JSON.stringify(value),
    });
  }
  return rows;
}

/** A skill's read view, with in-place editing of `SKILL.md` and its bundle files. */
export function SkillDetailScreen(props: {
  id: string;
  onBack: () => void;
  editing?: boolean;
}): JSX.Element {
  const resource = useSkillDetail(() => props.id);
  const skill = () => settled(resource) ?? undefined;
  const loadError = (): string | null =>
    resource.error
      ? skillErrorMessage(resource.error, "Could not load this skill.")
      : null;

  async function togglePublished(s: Skill): Promise<void> {
    const next = !s.published;
    try {
      await setSkillPublished(s.id, next);
      refreshRevisions();
      toast.success(
        next
          ? "Published — Talos and your agents can see this skill now."
          : "Unpublished — back to draft.",
      );
    } catch (err) {
      toast.error(
        skillErrorMessage(
          err,
          `Could not ${next ? "publish" : "unpublish"} the skill.`,
        ),
      );
    }
  }

  async function togglePinned(s: Skill): Promise<void> {
    const next = !s.pinned;
    try {
      await setSkillPinned(s.id, next);
      toast.success(
        next
          ? "Pinned — named in every conversation."
          : "Unpinned — found by search only.",
      );
    } catch (err) {
      toast.error(
        skillErrorMessage(
          err,
          `Could not ${next ? "pin" : "unpin"} the skill.`,
        ),
      );
    }
  }

  async function remove(s: Skill): Promise<void> {
    const ok = await confirm({
      title: `Delete "${s.name}"?`,
      detail:
        "The skill and every file in its bundle are permanently removed. Export it first if you want a copy.",
      confirmLabel: "Delete",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteSkill(s.id);
      toast.success(`Deleted "${s.name}"`);
      props.onBack();
    } catch (err) {
      toast.error(skillErrorMessage(err, `Could not delete "${s.name}"`));
    }
  }

  async function exportIt(s: Skill): Promise<void> {
    try {
      await exportSkill(s.id, s.name);
    } catch (err) {
      toast.error(skillErrorMessage(err, "Could not export the skill."));
    }
  }

  async function removeFile(s: Skill, path: string): Promise<void> {
    const ok = await confirm({
      title: `Delete ${path}?`,
      detail: "The file is permanently removed from the bundle.",
      confirmLabel: "Delete",
      tone: "alert",
    });
    if (ok) await deleteSkillFile(s.id, path);
  }

  return (
    <Suspense fallback={<LoadingText label="Loading skill" />}>
      <Show
        when={skill()}
        fallback={
          <EmptyState
            icon="layers"
            message={loadError() ? "Skill unavailable" : "Skill not found"}
            hint={loadError() ?? "The requested skill does not exist."}
            action={
              <Button
                variant="default"
                leading="chevron-left"
                onClick={props.onBack}
              >
                Back to skills
              </Button>
            }
          />
        }
      >
        {(s) => (
          <LibraryDetail
            backLabel="Skills"
            onBack={props.onBack}
            icon="layers"
            name={s().name}
            meta={`${s().source} · updated ${relativeTime(s().updatedAt)}`}
            flags={
              <>
                <Show when={s().source !== "authored"}>
                  <StatusFlag status={skillSourceFlag[s().source]}>
                    {s().source.toUpperCase()}
                  </StatusFlag>
                </Show>
                <StatusFlag status={skillStatusFlag(s().published)}>
                  {skillStatusLabel(s().published)}
                </StatusFlag>
                <Show when={s().pinned}>
                  <StatusFlag status="idle">PINNED</StatusFlag>
                </Show>
              </>
            }
            published={s().published}
            onTogglePublished={() => void togglePublished(s())}
            pinned={s().pinned}
            onTogglePinned={() => void togglePinned(s())}
            pinHint={PIN_HINT}
            onExport={() => void exportIt(s())}
            canEdit
            startEditing={props.editing}
            menuItems={[
              {
                label: s().published ? "Unpublish" : "Publish",
                icon: "check",
                onSelect: () => void togglePublished(s()),
              },
              {
                label: s().pinned ? "Unpin" : "Pin to brief",
                icon: "check",
                disabled: !s().published,
                hint: PIN_DRAFT_HINT,
                onSelect: () => void togglePinned(s()),
              },
              {
                label: "Delete",
                icon: "trash",
                danger: true,
                onSelect: () => void remove(s()),
              },
            ]}
            description={s().description}
            properties={() => propertiesOf(s())}
            files={filesOf(s())}
            saveFile={async (path, text) => {
              if (path === MAIN_FILE) {
                await putSkillDocument(s().id, text);
              } else {
                await putSkillFile(
                  s().id,
                  path,
                  new File([text], baseName(path)),
                );
              }
              refreshRevisions();
            }}
            history={
              <RevisionHistory
                base={revisionsBase("skills", s().id)}
                onRestored={refreshSkillDetail}
              />
            }
            onAddFile={async (relpath) => {
              await putSkillFile(
                s().id,
                relpath,
                new File([""], baseName(relpath)),
              );
            }}
            onDeleteFile={(path) => removeFile(s(), path)}
          />
        )}
      </Show>
    </Suspense>
  );
}
