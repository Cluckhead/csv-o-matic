# Page method: from material to page

How to turn source material into a page once the method (`explanation-method.md`) and the plan (`planning.md`) are clear. This covers tiers, the cast, product and feature explainers built on real outputs, worked cases, and reference material.

## Contents

1. Ground truth before prose
2. Choose the length tier
3. Build the cast
4. The page spine
5. Product and feature explainers
6. Real outputs, embedded and refreshable
7. Worked cases
8. Reference material
9. Outlines by tier

---

## 1. Ground truth before prose

An explanation is only as credible as the investigation behind it.

- **Read the real thing**: source code, API schema, documentation, data dictionary, incident timeline, spreadsheet. Use exact names of parameters, fields, statuses and errors.
- **Run it if you can.** For software, call the real product on realistic invented data and capture actual inputs and outputs. Real output catches documentation that has drifted from behaviour, and lets the reader operate something real instead of an animation that might teach behaviour the product does not have.
- **Keep a list of what you could not verify.** It becomes the "inferred, not observed" sentences and the assumptions table.
- Where no material exists (a topic from general knowledge), say so in the method section and tag figures *illustrative*.

## 2. Choose the shape and let the topic set the length

**A length the user states wins.** "Short", "one screen-ish", "a five-minute read", "about 2,000 words", "something the team can return to": treat it as the target. If the outcome cannot honestly be reached within it, build to the stated length, put the rest in `<details>` or a follow-on section, and say so in the reply.

Otherwise, **length is proportionate to the topic.** It comes from the plan, not from a quota:

1. List the ideas the reader must hold to reach the outcome statement, in dependency order (the sequence of models).
2. Give each the prose and the figure it needs: the question, the encoding, one action, the observation, the explanation, the limit.
3. Add only what the reader needs to act: what would change the conclusion, and for products the failures, worked cases and reference they will return to.
4. Stop. Do not pad to look substantial; do not cut a step the argument depends on to look brief.

A topic with one mechanism produces a short page even when there is more that could be said. A system with several interacting parts produces a long one even for a busy reader, and the skim layer (hero, headings, first sentences) is what serves the busy reader, not a shorter argument.

The shapes:

| Shape | When | What it contains |
|---|---|---|
| **Brief** | One decision, one mechanism, one incident | Hero with conclusion and 2–3 metrics; one or two figures; "what would change this"; method |
| **Explainer** | A mechanism or proposal the reader must understand to act | Hero; question-led sections, each with one small figure; a closing sandbox; boundary cases; method |
| **Field guide** | A product or system people will return to | A sequence of models, then failures, worked cases and reference |

When the material would support either of two shapes and the user has not said, choose the one that reaches the outcome with fewer steps and put optional depth in `<details>`.

## 3. Build the cast

Decide the cast before writing a word: a few named entities, each chosen to exhibit one behaviour.

- **Invent them, obviously.** Fictional companies, people, identifiers and figures that look realistic but belong to nobody. Never real companies or people, even in passing. Say once, near the top, that names and figures are invented.
- **One trait each.** "Halvorsen Aerospace: its code starts with a zero, and one upstream file lost it."
- **Never rename**, never restyle. The same identifier, label, row position and colour in every figure, table, example and conclusion.
- In a field guide, list the cast in a small table early. In a brief, introduce the one or two entities in the hero.

## 4. The page spine

Every tier shares one spine:

1. **Hero**: the conclusion in plain text, the outcome statement, two to four tagged figures.
2. **Note on the evidence**: what is invented, what is real.
3. **Teach the encoding** with the simplest case (often an event stepper with no model inputs).
4. **The sequence of models**: one question-led section per model, each with one small figure placed exactly where the reader has the context to interpret it.
5. **A prediction prompt and a boundary case**, where the rule just taught changes.
6. **The sandbox**, if the reader will benefit from combining what they learned. The one Midnight focal stage.
7. **What the model leaves out and what would change the conclusion**, visible, with method details in a disclosure.

Product explainers add reference material and worked cases after this spine (sections 5–8).

## 5. Product and feature explainers

The risk with a product is writing a manual: one panel per feature, a parameter table, some JSON. That documents the product but explains nothing. Instead:

- **Organise around the reader's question, not the feature list.** Ask what someone needs to predict when using the product ("will this search tell me the security is absent, or only that it hasn't found it yet?") and build the sequence of models around that.
- **Let each capability arrive as the answer to a demonstrated need** (*GPS*: complexity made necessary). Show the problem first ("a model that cannot read a 6 GB file needs to ask narrow questions"), then the capability that solves it.
- **Let the reader operate the real behaviour**, in miniature: a figure driven by the product's actual rule, or a stepper through real captured responses, with a few named cases.
- **Use real outputs as evidence beside the experiments.** After a figure shows how a rule behaves, a real response in an example viewer proves the product really does it.
- **Put the reference material last.** Parameter tables, field definitions and the full list of errors are for return visits; they follow the argument, they do not replace it.
- **Explain each part by its function** (*Mechanical Watch*): why does this tool or field exist? What would go wrong without it?

## 6. Real outputs, embedded and refreshable

- Write a small capture script that creates the invented data, runs each example through the real product, and writes `{id: {tool, arguments, response, is_error}}` to JSON.
- Embed it with `python scripts/embed_json.py <page>.html --id examples --json captured.json` and keep the script next to the page, so the examples refresh when the product changes and a reviewer can confirm they are genuine.
- Each `figure.ex` names an example id and the fields to show first (`data-keys="outcome,matches,explanation"`); the reader can expand to the whole response.
- Say in the caption when an example had to be contrived (an impossibly short time budget to show a timeout on a small file).
- Cover the unhappy paths: errors, empty results, partial results, malformed input. They are what users actually meet.

## 7. Worked cases

Worked cases turn reference into understanding, and are the natural home for persistent identities.

- Open with the **question as a user would ask it**.
- Then numbered steps, each with what the investigator wanted to know (muted), the call or action and its real result, and what was learnt.
- Close with a **verdict**: the conclusion, the evidence chain, and a sentence on anything inferred rather than observed.

## 8. Reference material

At the end of a field guide: compact tables of capabilities (needs → gives), fields and their meanings, limits and defaults, and a few rules of thumb. Link each item back to the section that explained it.

## 9. Outlines by tier

**Brief**
1. Hero with conclusion, outcome statement and 2–3 metrics.
2. One editorial band: the mechanism in three to five paragraphs; margin rail with definitions or a timeline.
3. One figure (two at most) on the decisive relationship, with a prediction prompt if the naive answer is wrong.
4. "What would change this": conditions and thresholds, visible.
5. Method and assumptions in a disclosure.

**Explainer** (the template follows this)
1. Hero.
2. Teach the encoding with the simplest case.
3. Model 2: the first single-control experiment, with a prediction prompt.
4. Model 3: a second single control on the same objects.
5. The boundary case as a comparison on one scale.
6. The sandbox, with a prediction prompt on an unfamiliar case.
7. Limits and what would change the conclusion.

**Field guide**
1. Hero with a "typical question" that previews a worked case.
2. The evidence note and the cast.
3. The one idea everything depends on, with its decoder or stepper.
4. The sequence of models, each capability arriving as the answer to a demonstrated need.
5. When things go wrong: failures, errors, malformed input.
6. Worked cases.
7. Reference.
