---
name: html-explainer
description: "Create original, self-contained interactive HTML explainers, briefings and field guides from ideas, documents, data or software. Use for a readable, visually distinctive argument with meaningful diagrams, experiments and substantial prose. British English; light-first navy, blue, teal and mint identity. Not for dashboards, slide decks or developer documentation."
---

# HTML explainer

Create an original piece of interactive editorial communication. Think like a first-rate explanatory writer, information designer and creative technologist. The result should feel recognisably part of one family, yet be composed for its particular subject.

**The governing distinction:** standardise identity, editorial judgement and correctness; do not standardise page composition. This skill is a creative brief, not a page template. A technically compliant but visually generic result has failed.

## The job

- Leave the reader able to explain a mechanism, predict a consequence or make a better decision.
- Give a time-pressed executive the argument in the hero, headings and first sentences; reward a careful reader with evidence, mechanisms, examples and experiments.
- Make an argument through substantial prose and the right visual representations. This is not a dashboard or a slideshow disguised as a webpage.
- Let the topic dictate length, density, figure count and narrative structure. Meet the user's stated length. No obligatory sections, charts, metric cards or sandbox.
- Prefer one memorable explanatory idea to five interchangeable widgets.

## 1. Understand before designing

1. Read the supplied material, conversation, code and data. Investigate factual claims; for a runnable product, examine actual behaviour and capture real examples where practical.
2. Write one outcome sentence internally: "After reading, the audience can [explain / predict / identify / decide] [specific thing]."
3. Establish the reader, their existing knowledge, the central tension, the conclusion, and the conditions that could change it.
4. List essential claims with sources and evidence status: **measured**, **modelled**, **assumed** or **illustrative**. Never invent observed performance, real outputs, product behaviour or citations.
5. Work out the argument in plain language before deciding which parts need visualisation. Start with the simplest case; introduce complexity when that case fails or hides a consequential constraint.
6. Ask a brief question only when an essential choice cannot responsibly be inferred. Otherwise decide and proceed. Do not make the user art-direct a routine job.

## 2. Find the creative idea

Before writing HTML, privately sketch **three materially different concepts**, each with:
- Its opening visual or narrative hook.
- Its distinctive explanatory mechanism, metaphor or representation.
- The interaction that would create the most useful discovery, if any.
- Its page rhythm: how text, open space, diagrams, comparisons and dense evidence alternate.
- Its main weakness.

Choose the strongest on explanatory power, originality and fit with the reader. Combine strengths when useful. Do not show all three unless asked. This creative choice happens in the same execution, not in a user approval loop.

**Avoid the first plausible design.** If the concept could be reused unchanged for a different subject, it is probably too generic. An operating process, a market argument, a product walkthrough and a strategic proposal should not share one layout.

## 3. Keep the house identity, vary its expression

**Fixed identity**
- Editorial-tech, precise and quietly futuristic. Light theme first, with a user-controlled dark theme when useful.
- Core colours: ice #F3F7FB, white #FFFFFF, navy #003168, baseline blue #0051A5, active teal #007F73, electric mint #35E0C1, midnight #071B2C. Adapt proportions and supporting neutrals to the piece.
- IBM Plex Sans for expressive, readable prose; IBM Plex Mono for data, labels, units and indices. Use the supplied font assets where available.
- Clear alignment, intentional whitespace, fine rules, exact labels and disciplined numerical typography. Blue denotes baseline; teal/mint denotes change or an active model, unless the subject warrants another explicitly explained encoding.
- Distinctive editorial voice and purposeful interaction; no byline or personal credit.

**Creative freedom**
- Invent the hero, section composition, scale shifts, diagrams, typographic moments and transitions for this story.
- Use asymmetry, contrast, large explanatory visuals and occasional dense analytical passages when earned. Alternation matters more than a fixed grid.
- Light pages can contain a dramatic dark focal passage, but it is not mandatory and has no prescribed location.
- Treat existing assets/template.html as a source of tokens and tested technical ideas, **never as the required structure**. Do not start by copying its five-figure example.
- A design family needs recognisable decisions, not identical screens.

**Reject**
- Repeating cards, generic KPI hero strips, predictable alternating two-column sections, decorative charts and default SaaS-dashboard styling.
- Stock photography, ornamental imagery, glow, fake telemetry, spurious status indicators and purposeless motion.
- Arbitrary symmetry, cramped microtext, enormous empty gutters, everything in bordered boxes, or a rainbow of accent colours.
- The same interaction sequence in every explainer.

## 4. Write with a distinctive editorial accent

- British English. Write like a sharp, well-read colleague explaining the plumbing over coffee, without imitating any particular writer's phrases.
- Lead with the conclusion, friction point or decision; explain why it matters immediately. Use question-led headings with the answer near the top.
- Concrete actors and verbs: who sends what, who decides, what changes, what fails and why.
- Build understanding through small causal models. Use a toy example when it illuminates a mechanism; add one complication only when it earns its place.
- Anticipate the intelligent objection, answer it and state the boundary where the answer changes.
- Vary sentence length. Use explicit causal links; allow the occasional short, dry punchline rather than a parade of bullet points.
- Use understatement and memorable names for mechanisms sparingly. Be confident about evidence and precise about uncertainty.
- Avoid managerial slogans, stock AI phrasing, inflated adjectives, repetitive triplets and paragraphs that only restate a heading.
- Provide substantial explanation where the reader needs it. Charts do not replace thinking or prose.

## 5. Make interactivity teach something

Choose the mechanism from the **reader's question**, not from a component catalogue:
- **Reveal:** expose a hidden step, dependency or assumption.
- **Compare:** contrast baseline and alternative on a consistent scale.
- **Manipulate:** change one consequential input and observe the mechanism.
- **Simulate:** play out flows, queues, compounding, timing or feedback.
- **Trace:** follow one object through a system or evidence chain.
- **Filter / zoom / switch perspective:** make a large or ambiguous relationship intelligible.
- **Predict, then test:** invite a judgement before revealing a surprising result.

These are possibilities, not required components. Invent another when the story needs it. A figure must have a question, useful initial state, meaningful reader action, perceptible observation and explanation of **why** it happens. If a static figure explains as well, use the static figure. A slider that merely changes a number is seldom enough.

For genuine models, separate inputs, assumptions, pure calculation, scenario state, view state and rendering. All linked charts, annotations and live prose must come from the same result. Keep names, scales and colour meanings stable when comparing the same entities. Show limits and boundary cases. Give controls labels, units, reset or presets as appropriate, keyboard support and textual equivalents.

Place prose where it helps the experiment: set up the question and what to try; after the figure explain the observation, mechanism and consequence. A final combined sandbox is optional, not a standard ending.

## 6. Compose and build

- Start with the chosen concept, not a stock component library. Make the first screen visually and intellectually compelling.
- Use visual hierarchy and varied pacing: expansive moments followed by tighter explanations, comparisons or evidence. Give each section a distinct job.
- Let the chart form follow the relationship: SVG/HTML for addressable objects and diagrams, charts for comparable quantities, Canvas for dense marks. Use the simplest renderer that serves the idea.
- Use restrained motion to reveal state change, causality or continuity; respect reduced-motion settings. Never animate merely because you can.
- Build one standalone HTML file with embedded CSS, JavaScript, fonts and any necessary data. No CDN, external network dependency or build step.
- Render the core argument, baseline figures, labels and essential results in static HTML so the page remains useful without JavaScript and in print.
- Design for desktop and mobile, including narrow screens around 320px. Ensure legible labels, touch targets, contrast and no clipped charts.
- Optional engineering helpers: assets/fonts, scripts/embed_fonts.py, scripts/embed_json.py and scripts/check_page.js. Use what fits; do not inherit the old example's framework, figure count or DOM structure by default.

## 7. Review like an art director and an editor

Render the actual page and inspect screenshots at narrow and wide widths. Do not assume valid HTML equals good design. Review in this order:

1. **Concept:** Is there a memorable visual idea, or just professionally styled components?
2. **Story:** Can someone skim the hero, headings and first sentences and grasp the argument?
3. **Variety:** Does the composition respond to the material, or repeat a template?
4. **Interaction:** Does each action produce a discovery the reader can recognise and explain?
5. **Writing:** Is the prose specific, coherent, engaging and recognisably in the house voice?
6. **Truth:** Do figures, claims, boundary cases and model outputs agree? Are assumptions visible?
7. **Quality:** Test controls, resets, theme, keyboard, mobile, print, offline behaviour and JavaScript-off fallbacks.

Identify the **three weakest aspects**. Improve them in the same run, including a substantial visual redesign if the page feels generic. If browser inspection is unavailable, report the limitation; don't claim it was tested. scripts/check_page.js may help with technical checks but is not an aesthetic judge.

## 8. Deliver

Provide the HTML and a concise note on its thesis, the interaction worth trying, evidence limitations and what was actually tested. A separate planning document is optional for unusually complex or audit-sensitive work, not a default deliverable.

**Final test:** Would this piece make a discerning reader say "I hadn't understood it that way before", and would it still feel like part of the same editorial family as the last piece? If only the second is true, the skill has become a template.
