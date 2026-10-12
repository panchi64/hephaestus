import { type JSX } from "solid-js";
import { LibrarySection } from "~/features/library";
import { SkillsDirectoryScreen } from "../screens/SkillsDirectoryScreen";
import { SkillDetailScreen } from "../screens/SkillDetailScreen";

/** Skills as one settings section: the directory, and the read view (with
 *  in-place editing) for whichever skill is open. See `LibrarySection` for why
 *  the open id is held there and not in the URL. */
export function SkillsSection(): JSX.Element {
  return (
    <LibrarySection
      directory={(nav) => (
        <SkillsDirectoryScreen onOpen={nav.view} onEdit={nav.edit} />
      )}
      detail={(id, nav) => (
        <SkillDetailScreen id={id} onBack={nav.back} editing={nav.editing} />
      )}
    />
  );
}
