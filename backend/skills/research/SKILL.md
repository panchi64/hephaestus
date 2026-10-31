---
name: research
description: Investigate a question across many sources with parallel researcher sub-agents and synthesize a cited narrative report. Use when the operator wants something genuinely investigated rather than answered from memory, such as comparing options, tracing a trend or history, sizing up a market, or reviewing studies.
---

# Research

You coordinate. Researchers read the web in parallel and report to you, and you write the report from their reports. Read something yourself only when a researcher isn't worth the round trip: a single lookup, or a gap you notice while writing.

## 1. Ask only what changes the search

Before anything else, check the request for ambiguity that would change what gets read: a term or acronym with several meanings, a question whose answer depends on a country or jurisdiction that isn't named, or a name that refers to more than one company, product or person. If there is one, ask once and briefly. Don't ask about anything else, and don't turn this into an intake form.

## 2. Plan the topics

Split the question into topics that don't overlap and together cover all of it. A topic is something one researcher can answer on its own. Where a topic benefits from independent points of view, give each view its own researcher.

| Researchers | When | Example |
|---|---|---|
| 1 | A single factual question | "Who won the 2024 Super Bowl?" |
| 3 | A focused topic with a few angles. This is the default. | Salmon migration and temperature: (1) biological mechanisms, (2) observed population data, (3) climate projections |
| 4–5 | Distinct facets or evaluation criteria, or several independent entities or regions | CRM platforms for small businesses: (1) pricing, (2) features, (3) integrations, (4) user sentiment |
| 6+ | A large enumerable set that needs systematic coverage | EV adoption by US state, one researcher per regional cluster |

Only three researchers run at once. The rest queue, so going wider mostly adds wall-clock time.

Enter plan mode and submit the topics as the plan, so the operator rules on what will be read before any reading starts. If you are extending earlier research (step 5), plan only what this request adds or needs re-checked.

## 3. Fan out

Once the plan is approved, launch one `researcher` per topic, all in a single step, then end your turn. Reports arrive on their own. Don't wait, and don't poll. If what you need from a researcher changes while it works, use `agents_message` to send it the amended brief rather than letting it finish the wrong thing.

The researcher never sees this conversation, so its brief has to carry everything. Name it for its topic and use this shape:

```
Research {the specific topic}.

Objective: {what the finished report must cover}

Key questions:
- {specific question}
- {specific question}

Suggested sources:
- {the kinds of source to prioritize, by name where you know them}

Constraints:
- {every bound from the operator's request: time period, place, scope}
```

For example:

```
Research the state of the semiconductor supply chain as of 2026.

Objective: The current situation, the fixes under way and the outlook, with
timelines and figures wherever they exist.

Key questions:
- Where are the bottlenecks now?
- How much capacity will the fabs under construction add, and when?
- Which geopolitical factors are shaping supply?
- When do analysts expect supply to meet demand?

Suggested sources:
- Quarterly reports from TSMC, Samsung and Intel (investor relations, SEC EDGAR)
- SEMI, Gartner and IDC forecasts
- CHIPS Act progress at commerce.gov and EU Chips Act updates at ec.europa.eu

Constraints:
- Current as of 2026. Mark anything older as background.
```

## 4. Take stock

When a report wakes you, call `agents_list` first and treat any researcher still working as reading you haven't seen.

Once every researcher has reported, check for gaps that would leave the answer incomplete. You may run **one** more round of researchers to close them, and only for gaps that serious, because every round delays the report. After that round, go to step 5 whatever comes back.

## 5. Title it and write it

Name the report: a short, specific title in the operator's language and in sentence case. It is 3–6 words and under 50 characters, made of letters, numbers and single spaces only. Keep accented and non-Latin letters as written. Use no hyphens, apostrophes or other punctuation. For example: "EU AI Act startup obligations".

List `~/scratch/reports/` first, and recall any report you have already delivered in this conversation. Then:

- For a new topic, choose a fresh title.
- For an extension or update of an earlier report, choose a title that names the topic and what this request adds ("EU AI Act startup penalties"). If nothing distinguishes it, use the earlier title with the next number ("EU AI Act startup obligations 2"). Read the earlier report as background and reuse what still holds. Where it disagrees with the new reports, the new reports win. The new report stands on its own and never refers the reader back.
- For an independent redo, choose a title as for an extension, but don't read the earlier report.

The title must differ from every existing report. Never overwrite or edit an earlier one.

Write the report to `~/scratch/reports/{title}.md` in the format `references/report-format.md` sets out. Read that file before you start writing. Writing a file doesn't create its folder, so on the first report create `reports/` with `files_create_directory`.

## 6. Deliver it

Call `view_show` with `file="reports/{title}.md"` and no `title`. The View picks the renderer from the file's `.md` name, and a `title` would replace that name. Then reply in three to five sentences: the key finding, what stays uncertain, and that the full report is open in the View.
