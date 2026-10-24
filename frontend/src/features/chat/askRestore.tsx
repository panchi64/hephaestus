import { choose } from "~/ui";
import { RewindRestoreBody } from "./components/RewindRestoreBody";
import type { RewindPreview } from "./data/rewind";
import type { RestoreChoice } from "./rewindRestore";

/** Put the file rewind to the operator in the shared confirmation dialog. */
export async function askRestore(
  preview: RewindPreview,
  changed: boolean,
): Promise<RestoreChoice> {
  const choice = await choose({
    title: "Restore files first?",
    detail:
      "Turns from here on changed files in your folders. They are still as the agent left them.",
    confirmLabel: "Restore files and continue",
    alternateLabel: "Continue without restoring",
    content: () => <RewindRestoreBody preview={preview} changed={changed} />,
  });
  return choice === "confirm"
    ? "restore"
    : choice === "alternate"
      ? "skip"
      : "cancel";
}
