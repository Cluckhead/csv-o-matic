# Planning: the work products before the page

Investigation and authorship come before the interface. Better graphics cannot repair a weak underlying investigation: you need a credible account of the system, in plain text, before choosing an action for the reader.

Write the plan to `<page-name>.plan.md` next to the page, and keep it. It lets the user (or a reviewer) check the reasoning, the model and the evidence before being persuaded by the presentation. Keep it short: a brief page's plan fits on one screen.

## Contents

1. The eight work products
2. Plan file template
3. The figure record
4. A complete worked example
5. Review: behaviour and understanding

---

## 1. The eight work products

1. **Audience and outcome statement.** Prior knowledge, the question being answered, and what the reader will be able to predict, decide or explain afterwards (see `explanation-method.md` §1).
2. **Claim and evidence map.** Each claim the page will make, tagged *measured*, *modelled*, *assumed* or *illustrative*, with the source that actually supports it. A source attached to a claim it does not support is worse than none.
3. **Conceptual sequence.** The questions in dependency order, and for each the limitation of the previous model that it repairs.
4. **Plain-text argument.** The explanation in a few paragraphs with no interaction at all. It must already state the main conclusion and its conditions. If the argument needs a figure to hide a missing step, the step is missing.
5. **Interaction plan.** One record per proposed figure (section 3). This is where weak figures are cut.
6. **Model specification.** Inputs with units and valid ranges, the rules, outputs, invariants, and boundary cases, before any styling. Write the model as a pure function and check it on the teaching cases by running it.
7. **Composed page.** Each figure placed where the reader has just enough context to interpret it, with a useful default and a one-sentence instruction.
8. **Review.** Of the reasoning and of the behaviour (section 5). Remove interactions that do not improve the explanation.

These stages order the *work*. The page itself presents the argument in the order easiest for the reader, which for executives means the conclusion first.

## 2. Plan file template

```markdown
# <Page name>: plan

## Reader and outcome
- Reader: <who, what they already know>
- Question they arrive with: <in their words>
- After reading, they can: <predict / decide / explain ...>
- Unfamiliar case to test that: <a case the page never shows directly, and its correct answer>

## Claims and evidence
| Claim | Status | Source |
|---|---|---|
| ... | measured / modelled / assumed / illustrative | file, run, person, or "invented for teaching" |

## Sequence of models
1. <Question> — simplest model: <...>
2. <Question> — what model 1 got wrong: <...>; adds: <...>
3. ...

## Argument in plain text
<Three to eight short paragraphs. No figures.>

## Figures
<One figure record per figure (section 3). List rejected figures too, with the reason.>

## Model
- Inputs: <name, unit, range, default, status>
- Rules: <plain statement, then the function name in the page>
- Outputs: <...>
- Invariants checked: <e.g. launch = max of branch finishes>
- Boundary cases: <where the conclusion changes, with the threshold>
- Teaching cases, run through the model: <case → result>
```

## 3. The figure record

Complete this for every figure **before coding it**. If you cannot fill in *Expected observation*, it is too early to build anything. If the *Static equivalent* communicates as well for this reader, use the static version.

| Field | What to write |
|---|---|
| Reader question | The one question this figure answers, from the vocabulary in `explanation-method.md` §7 |
| Initial case | The default state, chosen so the relationship is already visible |
| Action | The single thing the reader is invited to do |
| Expected observation | What they will see change, in one sentence |
| Explanation | Why it changes; the mechanism in one or two sentences |
| Comparison condition | What is held fixed, and on what shared scale |
| Boundary case | A setting where the rule changes, and what happens |
| Evidence status | Measured, modelled, assumed or illustrative, per input |
| Controls | At most two for a teaching figure; more only in the final sandbox |
| Accessible equivalent | Labelled input, keyboard path, text readout of the result |
| Static equivalent | What prints, and what a reader without JavaScript sees |

## 4. A complete worked example

The template page (`assets/template.html`) implements this plan. The numbers are invented to define a small teaching model; they are not delivery estimates.

**Reader and outcome.** Programme leads who own a launch date. They arrive asking "if we speed up a workstream, does launch come forward?" After reading they can **predict whether shortening a given activity moves launch, and recognise when a different branch takes over**. Unfamiliar case: "Validation slips by three days; does launch move?" (Yes, by three days: validation is on the controlling branch.)

**Model.** Design 4 days → Build 8 → Integration 4 → Validation 4, in sequence. Documentation 10 days, starting on day 0, independently. Launch needs both branches finished. So launch = max(4 + build + 4 + 4, documentation) = max(20, 10) = day 20. Assumes durations are known, dependencies are complete, and no shared people.

**Sequence of models.**
1. What decides the launch date? One timeline, two branches, one launch marker; teaches the encoding.
2. Does shortening documentation help? Only documentation can change. 10 → 5 days: launch stays at 20.
3. Does shortening build help? Only build can change. 8 → 4 days: launch moves to 16.
4. Is documentation simply unimportant? Boundary: documentation at 22 days now sets launch at 22.
5. Sandbox: both controls, the baseline held alongside on the same scale.

**One figure record** (figure 2):

| Field | Entry |
|---|---|
| Reader question | What changes when this input changes? |
| Initial case | Documentation 10 days; launch day 20 |
| Action | Reduce documentation to 5 days |
| Expected observation | Documentation's bar shrinks and its spare time grows; launch stays at day 20 |
| Explanation | Launch needs both branches; the slower branch still controls completion |
| Comparison condition | All other durations and dependencies fixed; one time axis, days 0–30 |
| Boundary case | Above 20 days, documentation becomes the controlling branch |
| Evidence status | Invented, illustrative |
| Controls | One slider: documentation duration, 1–28 days, step 1 |
| Accessible equivalent | Labelled slider; live sentence giving each branch's finish day and launch |
| Static equivalent | The baseline chart plus the sentence "At 5 days documentation finishes on day 5; launch stays at day 20" |

**A rejected figure.** "A slider for every activity at once, from the start" was rejected: it invites changing several things without knowing which caused the result. All controls appear together only in the closing sandbox.

## 5. Review: behaviour and understanding

**Behaviour** (what `scripts/check_page.js` and a quick model script cover):

- Each control changes the intended input; every linked view agrees; the live observation and the drawing come from one result.
- Presets reproduce their case exactly; reset restores the baseline.
- Minimum, maximum and threshold settings still produce meaningful output and true prose.
- The model's teaching cases match the numbers in the prose, hero and fallback text.

**Understanding** (do this yourself before delivering):

- Read only the hero, headings, first sentences and bold lines. Does the conclusion come through?
- Answer the plan's unfamiliar case using only what the page taught. If you need knowledge the page does not give, the sequence has a gap.
- For each figure: if a reader did the suggested action, would they notice the expected observation without being told twice? If not, change the default, the range or the prose.
- Can the reader tell an assumption from evidence everywhere on the page?
- Is there a figure that only shows a number changing? Replace it with a sentence or a table.
