import { createEffect, createSignal, Show, type JSX } from "solid-js";
import {
  Button,
  Input,
  LoadingText,
  Panel,
  Row,
  Stack,
  Text,
  Toggle,
  toast,
} from "~/ui";
import { inertBand } from "../contextBands";
import { saveChatSettings, useChatSettings } from "../data";
import {
  DEFAULT_SUBAGENT_LIMIT,
  DEFAULT_WALL_CLOCK_S,
  wallClockMinutes,
} from "../model";
import { NumberSetting, SettingHeader, wholeNumber } from "./NumberSetting";

const Rule = (): JSX.Element => <div class="border-line border-t" />;

/** Falls back to the shared seed for an unset bound, so switching the limit on and saving
 *  doesn't error on a blank field. */
function minutesOr(seconds: number | null): number {
  return wallClockMinutes(seconds ?? DEFAULT_WALL_CLOCK_S);
}

/* Operator preferences for how a turn runs. Each value seeds from the backend resource
   and saves back to it. */
export function ChatSection(): JSX.Element {
  const chatSettings = useChatSettings();
  // Off unless switched on: the step limit bounds a runaway turn, so this covers a run
  // that keeps emitting and never looks idle. Edited in minutes, stored in seconds.
  const [wallClockOn, setWallClockOn] = createSignal(false);
  const [wallClockMin, setWallClockMin] = createSignal("");
  const [savingWallClock, setSavingWallClock] = createSignal(false);
  // Off by default (null): a limit picked before the work's shape is known is mostly wrong.
  const [subagentCapOn, setSubagentCapOn] = createSignal(false);
  const [subagentCap, setSubagentCap] = createSignal("");
  const [savingSubagentCap, setSavingSubagentCap] = createSignal(false);
  // Stored as a fraction, edited as a percentage.
  const [autoCompactEnabled, setAutoCompactEnabled] = createSignal(true);
  const [autoCompactPct, setAutoCompactPct] = createSignal("");
  const [savingAutoCompact, setSavingAutoCompact] = createSignal(false);
  const [workSummaryMins, setWorkSummaryMins] = createSignal("");
  const [savingWorkSummary, setSavingWorkSummary] = createSignal(false);
  // Edited as percentages and saved together, because the pair is only valid in order.
  const [warnPct, setWarnPct] = createSignal("");
  const [alertPct, setAlertPct] = createSignal("");
  const [savingContext, setSavingContext] = createSignal(false);
  // Read from what is saved, not the inputs: a half-typed value would flicker the note.
  const inert = () => {
    const s = chatSettings();
    return s
      ? inertBand({
          foldEnabled: s.autoCompactEnabled,
          foldThreshold: s.autoCompactThreshold,
          alert: s.contextAlertThreshold,
        })
      : null;
  };
  createEffect(() => {
    const s = chatSettings();
    if (!s) return;
    setWallClockOn(s.wallClockTimeoutS !== null);
    setWallClockMin(String(minutesOr(s.wallClockTimeoutS)));
    setSubagentCapOn(s.subagentMaxConcurrent !== null);
    setSubagentCap(String(s.subagentMaxConcurrent ?? DEFAULT_SUBAGENT_LIMIT));
    setAutoCompactEnabled(s.autoCompactEnabled);
    setAutoCompactPct(String(Math.round(s.autoCompactThreshold * 100)));
    setWorkSummaryMins(String(s.workSummaryIdleMinutes));
    setWarnPct(String(Math.round(s.contextWarnThreshold * 100)));
    setAlertPct(String(Math.round(s.contextAlertThreshold * 100)));
  });
  const saveWallClock = async () => {
    // Off sends `null` (remove the bound), not an omission. A blank field can't block
    // turning it off.
    const minutes = wallClockOn() ? wholeNumber(wallClockMin()) : null;
    if (wallClockOn() && minutes === null) {
      toast.error("Enter whole minutes (1 or more).");
      return;
    }
    setSavingWallClock(true);
    try {
      const saved = await saveChatSettings({
        wallClockTimeoutS: minutes === null ? null : minutes * 60,
      });
      setWallClockOn(saved.wallClockTimeoutS !== null);
      setWallClockMin(String(minutesOr(saved.wallClockTimeoutS)));
      toast.success("Total time limit updated");
    } catch {
      toast.error("Unable to update the total time limit.");
    } finally {
      setSavingWallClock(false);
    }
  };

  const saveSubagentCap = async () => {
    // Off sends `null` (remove the cap), not an omission.
    const cap = subagentCapOn() ? wholeNumber(subagentCap()) : null;
    if (subagentCapOn() && cap === null) {
      toast.error("Enter a whole number of sub-agents (1 or more).");
      return;
    }
    setSavingSubagentCap(true);
    try {
      const saved = await saveChatSettings({ subagentMaxConcurrent: cap });
      setSubagentCapOn(saved.subagentMaxConcurrent !== null);
      setSubagentCap(
        String(saved.subagentMaxConcurrent ?? DEFAULT_SUBAGENT_LIMIT),
      );
      toast.success("Sub-agent limit updated");
    } catch {
      toast.error("Unable to update the sub-agent limit.");
    } finally {
      setSavingSubagentCap(false);
    }
  };

  const saveAutoCompact = async () => {
    // 0% would fire on an empty thread; 100% is a full window.
    const pct = wholeNumber(autoCompactPct(), { max: 100 });
    if (pct === null) {
      toast.error("Enter a whole percentage between 1 and 100.");
      return;
    }
    setSavingAutoCompact(true);
    try {
      const saved = await saveChatSettings({
        autoCompactEnabled: autoCompactEnabled(),
        autoCompactThreshold: pct / 100,
      });
      setAutoCompactEnabled(saved.autoCompactEnabled);
      setAutoCompactPct(String(Math.round(saved.autoCompactThreshold * 100)));
      toast.success("Auto-compaction updated");
    } catch {
      toast.error("Unable to update auto-compaction settings.");
    } finally {
      setSavingAutoCompact(false);
    }
  };
  const saveWorkSummary = async () => {
    // Floored at 1 so a thread isn't summarized between two turns of one sitting. The
    // ceiling is the backend's, repeated for immediate feedback.
    const mins = wholeNumber(workSummaryMins(), { min: 1, max: 1440 });
    if (mins === null) {
      toast.error("Enter a whole number of minutes between 1 and 1440.");
      return;
    }
    setSavingWorkSummary(true);
    try {
      const saved = await saveChatSettings({ workSummaryIdleMinutes: mins });
      setWorkSummaryMins(String(saved.workSummaryIdleMinutes));
      toast.success("Thread recap updated");
    } catch {
      toast.error("Unable to update the thread recap setting.");
    } finally {
      setSavingWorkSummary(false);
    }
  };

  const saveContextThresholds = async () => {
    // At 100 the amber band would be unreachable.
    const warn = wholeNumber(warnPct(), { max: 99 });
    const alert = wholeNumber(alertPct(), { min: 2, max: 100 });
    if (warn === null || alert === null) {
      toast.error("Enter whole percentages — warning 1–99, alert 2–100.");
      return;
    }
    // The backend owns this rule; checked here for immediate feedback. Equal boundaries
    // leave no amber band, inverted ones run the gauge backwards.
    if (warn >= alert) {
      toast.error("The warning percentage must be below the alert percentage.");
      return;
    }
    setSavingContext(true);
    try {
      const saved = await saveChatSettings({
        contextWarnThreshold: warn / 100,
        contextAlertThreshold: alert / 100,
      });
      setWarnPct(String(Math.round(saved.contextWarnThreshold * 100)));
      setAlertPct(String(Math.round(saved.contextAlertThreshold * 100)));
      toast.success("Context gauge updated");
    } catch {
      toast.error("Unable to update the context gauge.");
    } finally {
      setSavingContext(false);
    }
  };

  return (
    <Panel label="Chat">
      <Show when={chatSettings()} fallback={<LoadingText />}>
        <Stack gap={3}>
          <NumberSetting
            title="Inactivity timeout"
            stored={chatSettings()?.inactivityTimeoutS ?? null}
            placeholder="120"
            invalid="Enter whole seconds (1 or more)."
            noun="Inactivity timeout"
            save={async (n) =>
              (await saveChatSettings({ inactivityTimeoutS: n }))
                .inactivityTimeoutS
            }
          >
            How long a run may go without producing anything before it is
            stopped, in seconds. A long generation — a big file write, a long
            tool call — needs more than the default; raise it so a slow turn
            isn't cut off, or lower it to bail on a stuck one sooner.
          </NumberSetting>

          <Rule />

          <NumberSetting
            title="First-token wait"
            stored={chatSettings()?.firstTokenTimeoutS ?? null}
            placeholder="600"
            invalid="Enter whole seconds (1 or more)."
            noun="First-token wait"
            save={async (n) =>
              (await saveChatSettings({ firstTokenTimeoutS: n }))
                .firstTokenTimeoutS
            }
          >
            How long a turn waits, in seconds, for the model to start answering
            while it reads the prompt. A local model reading a long thread can
            be silent for minutes; the inactivity timeout does not count that
            wait. Raise it if long threads stop before the first word.
          </NumberSetting>

          <Rule />

          <SettingHeader title="Total time limit">
            A ceiling on how long one turn may take start to finish, however
            busy it is. Off by default: the step limit above is what stops a
            runaway turn, so a clock mostly cuts off turns that are simply slow
            — a local model, a long tool call. Switch it on to catch what
            neither other limit does: a turn that keeps producing output — a
            tool reporting progress, a model still writing — and so never looks
            idle, no matter how long it goes on.
          </SettingHeader>
          <Toggle
            checked={wallClockOn()}
            onChange={setWallClockOn}
            label="Stop a turn once it has run this long"
          />
          <Row gap={4} align="end">
            <Stack gap={1}>
              <Text variant="micro" tone="dim">
                STOP AFTER (minutes)
              </Text>
              <div class="w-48">
                <Input
                  type="number"
                  inputMode="numeric"
                  min="1"
                  value={wallClockMin()}
                  onInput={(e) => setWallClockMin(e.currentTarget.value)}
                  placeholder={String(minutesOr(null))}
                  disabled={!wallClockOn()}
                />
              </div>
            </Stack>
            <Button
              variant="primary"
              disabled={savingWallClock()}
              onClick={() => void saveWallClock()}
            >
              {savingWallClock() ? "Saving…" : "Save"}
            </Button>
          </Row>

          <Rule />

          <SettingHeader title="Sub-agents at once">
            The agent can hand pieces of work to sub-agents that go off and do
            them on their own, and it decides how many the work splits into. Up
            to 8 run at once unless you set a different limit — each is a
            separate agent spending model requests of its own, so this is the
            ceiling on how much can be running while you're not watching. Over
            the limit, the agent is told to wait rather than refused: nothing is
            lost, it just launches the next one as one finishes.
          </SettingHeader>
          <Toggle
            checked={subagentCapOn()}
            onChange={setSubagentCapOn}
            label="Set my own limit on how many sub-agents run at once"
          />
          <Row gap={4} align="end">
            <Stack gap={1}>
              <Text variant="micro" tone="dim">
                AT MOST
              </Text>
              <div class="w-48">
                <Input
                  type="number"
                  inputMode="numeric"
                  min="1"
                  value={subagentCap()}
                  onInput={(e) => setSubagentCap(e.currentTarget.value)}
                  placeholder={String(DEFAULT_SUBAGENT_LIMIT)}
                  disabled={!subagentCapOn()}
                />
              </div>
            </Stack>
            <Button
              variant="primary"
              disabled={savingSubagentCap()}
              onClick={() => void saveSubagentCap()}
            >
              {savingSubagentCap() ? "Saving…" : "Save"}
            </Button>
          </Row>

          <Rule />

          <SettingHeader title="Context gauge">
            The ring beside Send shows how full the model's context window is.
            It stays grey while there's room, turns amber at the first
            percentage, and red at the second — so it only catches your eye when
            it has something to say. Lower them if your turns are long enough
            that a nearly-full window runs out in one go; raise them if the ring
            colours earlier than you need it to.
          </SettingHeader>
          <Row gap={4} align="end">
            <Stack gap={1}>
              <Text variant="micro" tone="dim">
                AMBER AT (% of context)
              </Text>
              <div class="w-32">
                <Input
                  type="number"
                  inputMode="numeric"
                  min="1"
                  max="99"
                  value={warnPct()}
                  onInput={(e) => setWarnPct(e.currentTarget.value)}
                  placeholder="75"
                />
              </div>
            </Stack>
            <Stack gap={1}>
              <Text variant="micro" tone="dim">
                RED AT (% of context)
              </Text>
              <div class="w-32">
                <Input
                  type="number"
                  inputMode="numeric"
                  min="2"
                  max="100"
                  value={alertPct()}
                  onInput={(e) => setAlertPct(e.currentTarget.value)}
                  placeholder="90"
                />
              </div>
            </Stack>
            <Button
              variant="primary"
              disabled={savingContext()}
              onClick={() => void saveContextThresholds()}
            >
              {savingContext() ? "Saving…" : "Save"}
            </Button>
          </Row>
          {/* A band above the fold point is never reached on a thread with folding on.
              Stated here, where both numbers are editable, and never auto-corrected. */}
          <Show when={inert()}>
            {(band) => (
              <Text variant="micro" tone="dim">
                Note: threads fold at {band().fold}% of the window, so the red
                band at {band().at}% is only reached on a thread with
                auto-compaction switched off.
              </Text>
            )}
          </Show>

          <Rule />

          <SettingHeader title="Auto-compact conversations">
            When a conversation nears the model's context limit, its earlier
            turns are folded into a summary and the chat carries on instead of
            stopping. Your transcript keeps everything — only what the model
            re-reads is condensed, and the summary alone carries the thread from
            there. Off, a full conversation stops at the limit and you start a
            new one or rewind.
          </SettingHeader>
          <Toggle
            checked={autoCompactEnabled()}
            onChange={setAutoCompactEnabled}
            label="Fold older turns into a summary when the context fills"
          />
          <Row gap={4} align="end">
            <Stack gap={1}>
              <Text variant="micro" tone="dim">
                TRIGGER AT (% of context)
              </Text>
              <div class="w-32">
                <Input
                  type="number"
                  inputMode="numeric"
                  min="1"
                  max="100"
                  value={autoCompactPct()}
                  onInput={(e) => setAutoCompactPct(e.currentTarget.value)}
                  placeholder="80"
                  disabled={!autoCompactEnabled()}
                />
              </div>
            </Stack>
            <Button
              variant="primary"
              disabled={savingAutoCompact()}
              onClick={() => void saveAutoCompact()}
            >
              {savingAutoCompact() ? "Saving…" : "Save"}
            </Button>
          </Row>
        </Stack>

        <Stack gap={3}>
          <SettingHeader title="Thread recap">
            When you come back to a conversation after a while, a short account
            of what was done sits above the transcript. It is written once the
            thread has been quiet for the time below, so it never costs anything
            while you are still working, and it is rewritten from scratch each
            time the thread goes quiet again — there is only ever the newest
            one. The recap itself appears only after an hour away, on the
            grounds that anything sooner is still on screen.
          </SettingHeader>
          <Row gap={4} align="end">
            <Stack gap={1}>
              <Text variant="micro" tone="dim">
                WRITE IT AFTER (minutes idle)
              </Text>
              <div class="w-32">
                <Input
                  type="number"
                  inputMode="numeric"
                  min="1"
                  max="1440"
                  value={workSummaryMins()}
                  onInput={(e) => setWorkSummaryMins(e.currentTarget.value)}
                  placeholder="15"
                />
              </div>
            </Stack>
            <Button
              variant="primary"
              disabled={savingWorkSummary()}
              onClick={() => void saveWorkSummary()}
            >
              {savingWorkSummary() ? "Saving…" : "Save"}
            </Button>
          </Row>
        </Stack>
      </Show>
    </Panel>
  );
}
