import { type JSX } from "solid-js";
import { Input, Stack, Textarea } from "~/ui";
import { charCount } from "../model";

/** Which fields a rejection can name. */
export type IdentityFieldErrors = Partial<
  Record<"name" | "description", string>
>;

/** The two fields that identify a skill or an agent, with live character
 *  counts. Shared by both create dialogs and both editors' DETAILS panels. The
 *  counts mirror the backend's rules for display only: the server's 422 message
 *  replaces the count and renders verbatim, and nothing here gates a submit. */
export function IdentityFields(props: {
  name: string;
  description: string;
  onNameInput: (value: string) => void;
  onDescriptionInput: (value: string) => void;
  namePattern: RegExp;
  /** Tail of the hint shown beside the count when the name is malformed. */
  patternHint: string;
  nameMax?: number;
  descriptionMax: number;
  namePlaceholder: string;
  descriptionPlaceholder: string;
  errors?: IdentityFieldErrors;
  descriptionRows?: number;
  disabled?: boolean;
  autofocus?: boolean;
}): JSX.Element {
  const nameMalformed = () =>
    props.name !== "" && !props.namePattern.test(props.name);
  const nameInvalid = () =>
    Boolean(props.errors?.name) ||
    (props.nameMax !== undefined && props.name.length > props.nameMax) ||
    nameMalformed();
  const nameHint = () => {
    if (props.errors?.name) return props.errors.name;
    const count = charCount(props.name, props.nameMax);
    return nameMalformed() ? `${count} · ${props.patternHint}` : count;
  };

  const descriptionInvalid = () =>
    Boolean(props.errors?.description) ||
    props.description.length > props.descriptionMax;
  const descriptionHint = () =>
    props.errors?.description ??
    charCount(props.description, props.descriptionMax);

  return (
    <Stack gap={3}>
      <Input
        label="Name"
        value={props.name}
        autofocus={props.autofocus}
        disabled={props.disabled}
        invalid={nameInvalid()}
        hint={nameHint()}
        placeholder={props.namePlaceholder}
        onInput={(e) => props.onNameInput(e.currentTarget.value)}
      />
      <Textarea
        label="Description"
        value={props.description}
        disabled={props.disabled}
        invalid={descriptionInvalid()}
        hint={descriptionHint()}
        placeholder={props.descriptionPlaceholder}
        rows={props.descriptionRows ?? 3}
        onInput={(e) => props.onDescriptionInput(e.currentTarget.value)}
      />
    </Stack>
  );
}
