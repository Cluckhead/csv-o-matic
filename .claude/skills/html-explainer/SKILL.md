---
name: html-explainer
description: Builds interactive HTML explanations in the user's house style, in the manner of Bartosz Ciechanowski and Josh Comeau - a readable argument made of small experiments, where each figure asks one question, the reader makes one change, and the prose says what to notice and why - written for executives (conclusion first, British English, a Matt Levine / Ethan Mollick voice) in a navy, teal and mint IBM Plex design that opens offline from one file. Use it whenever the user wants something that explains a product, feature, tool, system, process, project, incident, proposal, funding case or complex topic, short or long, and whenever they ask to turn notes, documents, code, data or a conversation into an explainer, guide, one-pager, briefing, walkthrough or "something I can send round", even if they never say HTML. Asks targeted multiple-choice questions when key facts are missing. Not for slide decks, Word or PDF documents, live-data dashboards, or developer docs that belong in a repository.
---

# HTML explainer

Make one standalone HTML page that leaves its reader able to **explain a mechanism, predict a consequence or assess a proposal**. It is a readable argument with a few carefully chosen experiments, not a dashboard: a reader changes one thing, sees the consequence, and is told enough to interpret it. Quality comes from the fit between the question, the model, the action and the prose, never from the number of controls.

Two traditions meet here. **Bartosz Ciechanowski** for how to construct an explanation: a chain of increasingly capable models, the same objects kept on screen while one condition changes, the visual language taught before it is relied on, and prose written around each act of observation. **Josh Comeau** for how to compose and maintain it: substantial writing around purpose-built demonstrations, one parameter introduced at a time before a combined sandbox, and a small reusable framework underneath. The page itself belongs to the user: their prose style, their visual identity, no byline.

## What a good page does

- States its conclusion in the hero in plain text, plus an outcome statement: what the reader will be able to do afterwards.
- Answers a **sequence of questions in dependency order**. Each section's model repairs a limitation of the one before.
- Has **small figures in the reading flow**, each answering one question with **one or two controls**. All controls appear together only in a closing sandbox, after each was met alone.
- Surrounds every figure with prose. Before it: the question, any new encoding, and one suggested action. After it: what to notice, why it happens, and where it stops applying.
- Keeps the **same cast and encodings** from first figure to last.
- Includes at least one **prediction prompt** and one **boundary case** where the rule just taught changes.
- Tags every number as *measured*, *modelled*, *assumed* or *illustrative*, and never lets a smooth model make an assumption look like a fact.
- Opens offline in Light theme, works from 320 px phones to 27-inch screens, and still reads with JavaScript off.

## Files

| File | Read it when |
|---|---|
| `references/explanation-method.md` | **First, always.** The method: outcome statements, sequences of models, writing around observation, the interaction vocabulary, when an interaction earns its space, moves borrowed from specific articles |
| `references/planning.md` | Before building: the eight work products, the plan file, the per-figure record, review of behaviour and understanding |
| `references/questions.md` | When something only the user can decide is missing |
| `references/page-method.md` | Tiers, the cast, real outputs for product explainers, worked cases, reference sections |
| `references/prose-style.md` | Before writing any copy. The user's own style guide; follow it closely |
| `references/interactive-models.md` | Before building figures: the component catalogue, state separation, renderer choice, time, dragging, performance, accessibility |
| `references/visual-system.md` | When extending the look beyond the template |
| `assets/template.html` | The starting point: a complete worked example of the method, plus the framework |
| `scripts/embed_fonts.py`, `embed_json.py`, `check_page.js`, `make_fragment.py` | Fonts, captured data, the browser check, the Artifact-ready copy |

## Workflow

### 1. Settle the brief

Read everything available: the request, attachments, the conversation, and any code or data the page will describe. Then write the **outcome statement**:

> After this page, the reader can *[predict / decide / explain / identify]* … *[something specific and checkable]*.

Also settle the audience, the document type (feature, problem, proposal, project, observed data, topic), the shape (brief, explainer or field guide) and any length the user stated, and what ground truth exists.

If something only the user can decide is missing, or a wrong guess would waste a long build, ask: one round, at most four multiple-choice questions generated from the material in hand, recommendation first (`references/questions.md`). Use `AskUserQuestion` where it exists; otherwise ask inline. If nothing important is missing, ask nothing, state the brief in three or four lines including the outcome statement, and carry on.

### 2. Investigate and plan

Better graphics cannot repair a weak investigation. Get a credible account of the system in plain text before choosing any action for the reader. If the subject is software you can run, run it on realistic invented data and capture real outputs.

Then write `<page-name>.plan.md` next to the page (`references/planning.md`):

- the outcome, plus an **unfamiliar test case** and its answer;
- claims with their evidence status;
- the **sequence of models**: each question, and what the previous model got wrong;
- the argument in plain text, with no figures;
- **one record per figure**: reader question, initial case, action, expected observation, explanation, what is held fixed, boundary case, evidence status, static equivalent;
- the model specification, run on the teaching cases.

Cut every figure whose expected observation is just "a number changes", or whose static equivalent teaches as well. Say in the plan why it was cut.

### 3. Start from the template

Copy `assets/template.html` to the output path and run `python scripts/embed_fonts.py <page>.html`. The template is a complete example of the method. A two-branch project plan is explained in five figures:

1. an event stepper that teaches the encoding;
2. one control, after a prediction prompt;
3. a different single control on the same objects;
4. a boundary case as a linked comparison on one scale;
5. a closing sandbox in the single Midnight stage.

It also carries the framework: `Demo.create`, presets, prose triggers, reset, event stepping, drag handles, prediction prompts and example viewers.

Keep the framework and the visual system; replace the example. Delete what you do not use. Keep `<title>` first after the `PAGE:HEAD` marker, and give the page a real two-to-five-word name.

### 4. Write

Follow `references/prose-style.md`, and `references/explanation-method.md` §5 for the passages around figures:

- Open with the friction point or decision. Use question-led headings, each section's answer in its first sentence.
- Before a figure: the question, any new encoding, and one action, often as an inline trigger.
- After a figure: the observation, the mechanism, the trade-off or limit.
- Keep prose true after interaction: anchor sentences to named cases or write rules that hold across the range. The live line under each figure comes from the same model result as the drawing.
- Voice: a sharp colleague who has read everything. Toy models and deadpan understatement from Levine; real examples, named frameworks and honest caveats from Mollick. British English, active verbs, one bold insight per key paragraph.
- Never hide a condition that could reverse the conclusion in a collapsed panel.

### 5. Build the figures

Follow `references/interactive-models.md`. One pure `model()` for the page where possible, so every figure shows the same objects. One renderer with options per figure. `Demo.create` per figure, with only the parameters that figure is about.

Every figure gets:

- a useful default in which the relationship is already visible;
- a reset;
- a `.live` line computed from the model;
- a `.fig-summary` in the static HTML stating the teaching case's result, worked out by running the model.

Use `figure.ex` viewers with embedded real outputs (`scripts/embed_json.py`) as evidence beside the experiments in product explainers, not instead of them.

### 6. Review

- Run `node scripts/check_page.js <page>.html --out <dir>` once. It covers:
  - errors, network calls, overflow and clipped text;
  - Light-first opening and embedded fonts;
  - every slider, preset and prediction prompt;
  - the theme switch keeping state, print, and JavaScript off.
  Look at the screenshots.
- Run the model on the teaching cases and boundaries in a quick script, and compare with every number in the prose, hero and fallback text.
- Review understanding (`references/planning.md` §5):
  - answer the plan's unfamiliar case using only what the page taught;
  - read only the hero, headings and bold lines;
  - for each figure, ask whether the expected observation would actually be noticed.
- Fix in one pass. If Playwright is unavailable, say so rather than installing it uninvited.

### 7. Deliver

- Save the standalone `.html` and its `.plan.md` where the user can open them.
- If an Artifact tool is available and the page is to be shared, publish the copy made by `python scripts/make_fragment.py <page>.html -o <page>.artifact.html`.
- Reply briefly with:
  - the location and link;
  - the outcome statement and the thesis;
  - anything inferred or illustrative that needs confirming;
  - what was checked.

## Length

Length follows from the topic, not from a word count. **If the user states a length** (words, minutes, screens, pages, "short", "one screen-ish"), that is the target; meet it and say in the reply if the outcome needed more. Otherwise the page is as long as the sequence of models the outcome requires: count the ideas the reader must hold, in dependency order, give each one the prose and figure it needs, and stop. A one-mechanism topic is short even if there is plenty more to say; a system with six interacting parts is long even if the reader is busy. Never pad to reach a size or cut a step the argument depends on to stay under one.

The tiers name a shape, not a size:

- **Brief**: hero, one or two figures, "what would change this", method.
- **Explainer**: question-led sections, one small figure each, a closing sandbox.
- **Field guide**: a long sequence of models, then failures, worked cases and reference (`references/page-method.md`).

## House rules

- One file with everything embedded. No CDNs, no network calls, no build step.
- Light theme on first open, with a visible Light/Dark switch that keeps all state.
- No byline, watermark, stock imagery, decoration, glow, fake telemetry or looping animation.
- Blue means baseline; teal or mint means the thing the reader controls or a modelled result. At most one Midnight focal stage, usually the closing sandbox.
- IBM Plex Sans for prose and headings; IBM Plex Mono with tabular numbers for figures, units, axes and indices.
- Invented cast, named identically everywhere. Never invent product behaviour; use real outputs where the subject can be run.
