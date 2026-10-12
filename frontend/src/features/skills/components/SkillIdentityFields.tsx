import { type JSX } from "solid-js";
import { IdentityFields, type IdentityFieldErrors } from "~/features/library";
import {
  SKILL_DESCRIPTION_MAX,
  SKILL_NAME_MAX,
  SKILL_NAME_PATTERN,
} from "../model";

export type SkillFieldErrors = IdentityFieldErrors;

/** The two fields that identify a skill, under the skills' own rules. */
export function SkillIdentityFields(props: {
  name: string;
  description: string;
  onNameInput: (value: string) => void;
  onDescriptionInput: (value: string) => void;
  errors?: SkillFieldErrors;
  disabled?: boolean;
  autofocus?: boolean;
}): JSX.Element {
  return (
    <IdentityFields
      {...props}
      namePattern={SKILL_NAME_PATTERN}
      patternHint="lowercase letters, numbers, and hyphens only"
      nameMax={SKILL_NAME_MAX}
      descriptionMax={SKILL_DESCRIPTION_MAX}
      namePlaceholder="summarize-document"
      descriptionPlaceholder="What this skill does and when the agent should reach for it."
    />
  );
}
