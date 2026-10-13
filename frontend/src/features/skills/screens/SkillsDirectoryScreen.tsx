import { createSignal, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { StatusFlag, Text, confirm, toast } from "~/ui";
import {
  compareByName,
  compareByUpdated,
  LibraryDirectory,
} from "~/features/library";
import { bytes, relativeTime } from "~/lib/format";
import {
  deleteSkill,
  exportSkill,
  importSkill,
  setSkillPinned,
  setSkillPublished,
  skillErrorMessage,
  useSkills,
} from "../data";
import {
  PIN_DRAFT_HINT,
  skillSourceFlag,
  skillStatusFlag,
  skillStatusLabel,
  type SkillSummary,
} from "../model";
import { BuiltInSkillsSection } from "../components/BuiltInSkillsSection";
import { NewSkillDialog } from "../components/NewSkillDialog";

export function SkillsDirectoryScreen(props: {
  /** Open a skill's read view. The directory lives inside the settings dialog,
   *  which owns that second level, so where "open" goes is the caller's
   *  business rather than a route this component hard-codes. */
  onOpen: (id: string) => void;
  /** Open a skill straight into editing. */
  onEdit: (id: string) => void;
}): JSX.Element {
  const skillsResource = useSkills();
  // Reading a Solid resource accessor re-throws its error (same hazard GalleryScreen
  // documents). A 500 from /skills would otherwise trip the shell's ErrorBoundary and
  // replace the whole page with one message — InstrumentBand reads this and sits outside
  // the Suspense. Derive from `settled` so a failed load degrades to an empty
  // library with a message, and the boundary stays the net rather than the plan.
  const skills = (): SkillSummary[] => settled(skillsResource) ?? [];
  const loadError = (): string | null =>
    skillsResource.error
      ? skillErrorMessage(skillsResource.error, "Could not load skills.")
      : null;
  const [newOpen, setNewOpen] = createSignal(false);
  const [importing, setImporting] = createSignal(false);

  // ── import ──────────────────────────────────────────────────────────────

  async function handleImport(file: File | undefined): Promise<void> {
    if (!file || importing()) return;
    setImporting(true);
    try {
      const { skill, warnings } = await importSkill(file);
      toast.success(`Imported "${skill.name}" as a draft`, {
        action: {
          label: "Open",
          onClick: () => props.onOpen(skill.id),
        },
      });
      // Everything the backend flagged about the bundle, verbatim — the operator
      // reads these before deciding to publish.
      for (const warning of warnings) toast.warn(warning);
    } catch (err) {
      toast.error(skillErrorMessage(err, "Could not import the bundle."));
    } finally {
      setImporting(false);
    }
  }

  // ── row actions ─────────────────────────────────────────────────────────

  async function handleDelete(skill: SkillSummary): Promise<void> {
    const ok = await confirm({
      title: `Delete "${skill.name}"?`,
      detail:
        "The skill and every file in its bundle are permanently removed. Export it first if you want a copy.",
      confirmLabel: "Delete",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deleteSkill(skill.id);
      toast.success(`Deleted "${skill.name}"`);
    } catch (err) {
      toast.error(skillErrorMessage(err, `Could not delete "${skill.name}"`));
    }
  }

  async function handlePublishToggle(skill: SkillSummary): Promise<void> {
    const next = !skill.published;
    try {
      await setSkillPublished(skill.id, next);
    } catch (err) {
      toast.error(
        skillErrorMessage(
          err,
          `Could not ${next ? "publish" : "unpublish"} "${skill.name}"`,
        ),
      );
      return;
    }
    toast.success(
      next
        ? `"${skill.name}" published — the agent can see it now.`
        : `"${skill.name}" unpublished — back to draft.`,
      {
        action: {
          label: "Undo",
          onClick: () => {
            setSkillPublished(skill.id, !next).catch(() =>
              toast.error("Could not undo"),
            );
          },
        },
      },
    );
  }

  async function handlePinToggle(skill: SkillSummary): Promise<void> {
    const next = !skill.pinned;
    try {
      await setSkillPinned(skill.id, next);
    } catch (err) {
      toast.error(
        skillErrorMessage(
          err,
          `Could not ${next ? "pin" : "unpin"} "${skill.name}"`,
        ),
      );
      return;
    }
    toast.success(
      next
        ? `"${skill.name}" pinned — named in every conversation.`
        : `"${skill.name}" unpinned — found by search only.`,
      {
        action: {
          label: "Undo",
          onClick: () => {
            setSkillPinned(skill.id, !next).catch(() =>
              toast.error("Could not undo"),
            );
          },
        },
      },
    );
  }

  async function handleExport(skill: SkillSummary): Promise<void> {
    try {
      await exportSkill(skill.id, skill.name);
    } catch (err) {
      toast.error(skillErrorMessage(err, `Could not export "${skill.name}"`));
    }
  }

  // ── derived ─────────────────────────────────────────────────────────────

  const totalBytes = () => skills().reduce((sum, s) => sum + s.sizeBytes, 0);

  return (
    <LibraryDirectory
      title="Skills"
      subtitle="Agent Skills bundles — reusable procedures the assistant can follow."
      assetId="HEPH-SKL-01.0"
      noun="skill"
      nouns="skills"
      icon="layers"
      items={skills()}
      loadError={loadError()}
      sorts={{
        recent: { label: "Newest", compare: compareByUpdated },
        name: { label: "Name", compare: compareByName },
      }}
      initialSort="recent"
      initialDir="desc"
      bandExtra={[{ label: "Bundles", value: bytes(totalBytes()) }]}
      draftHint="A draft is invisible to the agent. Publishing is what makes a skill's instructions something the assistant will follow — imported bundles always land as drafts so you can read them first."
      importHint="Accepts an Agent Skills bundle (.zip) or a lone SKILL.md. The bundle keeps its supporting files, and anything unusual about it is reported as a warning."
      importAccept=".zip,.md"
      importing={importing()}
      onImport={(file) => void handleImport(file)}
      onNew={() => setNewOpen(true)}
      onOpen={(skill) => props.onOpen(skill.id)}
      emptyCreateHint="Create a skill or import an Agent Skills bundle."
      rowRight={(skill) => (
        <>
          {/* Shown for any bundle that has files at all — a row that drops the
              field at one file would make the column jump. */}
          <Show when={skill.fileCount > 0}>
            <Text variant="micro" tone="dim">
              {skill.fileCount} {skill.fileCount === 1 ? "File" : "Files"} ·{" "}
              {bytes(skill.sizeBytes)}
            </Text>
          </Show>
          <Show when={skill.source !== "authored"}>
            <StatusFlag status={skillSourceFlag[skill.source]}>
              {skill.source.toUpperCase()}
            </StatusFlag>
          </Show>
          <StatusFlag status={skillStatusFlag(skill.published)}>
            {skillStatusLabel(skill.published)}
          </StatusFlag>
          <Show when={skill.pinned}>
            <StatusFlag status="idle">PINNED</StatusFlag>
          </Show>
          <Text variant="micro" tone="dim">
            {relativeTime(skill.updatedAt)}
          </Text>
        </>
      )}
      menuItems={(skill) => [
        {
          label: "Edit",
          icon: "edit",
          onSelect: () => props.onEdit(skill.id),
        },
        {
          label: "Export",
          icon: "download",
          onSelect: () => void handleExport(skill),
        },
        {
          label: skill.published ? "Unpublish" : "Publish",
          icon: "check",
          onSelect: () => void handlePublishToggle(skill),
        },
        {
          label: skill.pinned ? "Unpin" : "Pin to brief",
          icon: "check",
          disabled: !skill.published,
          hint: PIN_DRAFT_HINT,
          onSelect: () => void handlePinToggle(skill),
        },
        {
          label: "Delete",
          icon: "trash",
          danger: true,
          onSelect: () => void handleDelete(skill),
        },
      ]}
    >
      <NewSkillDialog
        open={newOpen()}
        onClose={() => setNewOpen(false)}
        onCreated={(id) => {
          setNewOpen(false);
          props.onEdit(id);
        }}
      />
      <BuiltInSkillsSection />
    </LibraryDirectory>
  );
}
