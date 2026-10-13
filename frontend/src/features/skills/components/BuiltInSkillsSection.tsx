import { For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Disclosure,
  Markdown,
  Panel,
  StatusFlag,
  Text,
  Toggle,
  toast,
} from "~/ui";
import {
  setBuiltInSkillEnabled,
  skillErrorMessage,
  useBuiltInSkills,
} from "../data";
import { SHADOWED_HINT, type BuiltInSkill } from "../model";

async function handleToggle(skill: BuiltInSkill): Promise<void> {
  const next = !skill.enabled;
  try {
    await setBuiltInSkillEnabled(skill.name, next);
  } catch (err) {
    toast.error(
      skillErrorMessage(
        err,
        `Could not ${next ? "enable" : "disable"} "${skill.name}"`,
      ),
    );
  }
}

/** The skills shipped with the app. Read-only: a row can be switched on or off
 *  and opened to read, never edited or deleted. */
export function BuiltInSkillsSection(): JSX.Element {
  const resource = useBuiltInSkills();
  // A failed load leaves the section out rather than tripping the boundary.
  const skills = (): BuiltInSkill[] => settled(resource) ?? [];

  return (
    <Show when={skills().length > 0}>
      <Panel
        label="Built in"
        meta={
          <Text variant="meta" tone="dim">
            {skills().length}
          </Text>
        }
        flush
      >
        <For each={skills()}>
          {(skill) => (
            <div class="px-4 py-2">
              <Disclosure
                label={skill.name}
                labelNode={
                  <span class="flex min-w-0 flex-1 flex-col items-start overflow-hidden">
                    <Text
                      variant="body"
                      tone="bright"
                      class="block w-full truncate"
                    >
                      {skill.name}
                    </Text>
                    <Text
                      variant="label"
                      tone="dim"
                      class="block w-full truncate"
                    >
                      {skill.description}
                    </Text>
                    <Show when={skill.shadowed}>
                      <Text
                        variant="micro"
                        tone="dim"
                        class="block w-full truncate"
                      >
                        {SHADOWED_HINT}
                      </Text>
                    </Show>
                  </span>
                }
                trailing={
                  <Show when={skill.shadowed}>
                    <StatusFlag status="idle">SHADOWED</StatusFlag>
                  </Show>
                }
                actions={
                  <Toggle
                    checked={skill.enabled}
                    onChange={() => void handleToggle(skill)}
                  />
                }
                rowClass="gap-3"
                class="mt-2"
              >
                <div class="scrollbar-thin max-h-[28rem] overflow-auto">
                  <Markdown dense>{skill.body}</Markdown>
                </div>
              </Disclosure>
            </div>
          )}
        </For>
      </Panel>
    </Show>
  );
}
