# Reviewing a first-pass grade

A first-pass grader has scored one run by `GRADING.md`. Your job is to find the grades that break that standard and correct them. You don't grade the run a second time. A reviewer who re-scores by taste turns a second opinion into a second sample of noise, and the agreement figure stops meaning anything.

## Before you review

1. Read `docs/voice.md` §4–§13 and `GRADING.md` in full.
2. Read the run's `transcript.md`, then its `grades.json`.
3. Stay blind, as the grader was: don't open the group's `key.json` or `prompt.md`, or any other run's files.

## What counts as a misgrade

Correct a grade when you can point to the rule it breaks and the transcript words that show it:

- **Anchors.** The score sits at a level whose anchor plainly doesn't describe the sample. For example, a 3 on `character` for a reply that clips into log fragments, or a 1 on `boilerplate` with no stock phrase quoted.
- **Lint caps.** A confirmed lint flag left uncapped, or a misfired flag (one of the dismissals `GRADING.md` lists) used to cap a dimension.
- **Evidence.** The evidence doesn't say what the grader claims, or it isn't in the transcript.
- **Humor list.** A humorous line in Talos's final replies is missing from the list. Or a listed line is plain prose. Or its `faults` miss a `stock`, `repeat`, `dark` or `mean` the rubric defines. Or a `humor` score of 3 sits on a sample with a faulted line.
- **Expect items.** A pass for something the transcript doesn't show, or a fail for something it does.
- **The voice/work line.** The voice was graded down for work correctness, outside an expect item or `judgment`.
- **`worst`.** It skips a reply that clearly belongs above one it lists, or quotes words not in the transcript.

## What doesn't

- A score one level off where the anchors overlap and the grader's evidence fits the level chosen. Leave it.
- A different but equally valid reading of a judgment call.
- Wording of notes or evidence that is accurate but not how you'd put it.

When unsure, leave the grade. A correction has to be one any careful reader of `GRADING.md` would agree with.

## What you write

Write `review.json` into the run directory. It must validate against `Review` in `evals/voice/review.py`:

```json
{
  "reviewer": "<model and date, e.g. claude-opus-5-5 2026-11-01>",
  "dimensions": [
    {"scenario": "banter", "sample": 2, "dimension": "humor", "score": 1,
     "evidence": "\"Even the server needs its beauty sleep.\"",
     "reason": "GRADING.md humor 1: stock sleep line; first pass scored 3"}
  ],
  "expect": [
    {"scenario": "wal-calibration", "sample": 1, "item": 2, "passed": false,
     "note": "ends with an offer", "reason": "the last sentence is an offer to benchmark"}
  ],
  "humor": null,
  "humor_reason": "",
  "worst": null,
  "notes": null,
  "rule_gaps": ["GRADING.md doesn't say whether a returned 'good night' counts as a sign-off under boilerplate"]
}
```

- **Corrections only.** `dimensions` and `expect` list only what you correct. Each `dimension` correction gives the new score with its evidence (required below 3). Each correction needs a `reason` naming the rule.
- **`expect` items** count from 1, in the scenario's order.
- **Replacements.** `humor`, `worst` and `notes` stay `null` unless you replace them. A replaced `humor` is the whole corrected list, and `humor_reason` says what changed.
- **`rule_gaps`.** Name places where `GRADING.md` itself was unclear enough to cause a misgrade, as one sentence each. These feed the calibration pass, so name the gap, not the grade.
- **Empty review.** A review with nothing to correct is valid: empty lists and `null`s.

Then apply it, and fix `review.json` until it passes:

```bash
cd backend && uv run python -m evals.voice.review <group>/<letter>
```
