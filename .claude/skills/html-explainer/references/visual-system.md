# Visual system

The look is an editorial-grade analytical workspace: comfortable long-form reading beside live quantitative models, causal diagrams and scenario controls. It is designed first for 27-inch widescreen displays (2560 × 1440 and 3840 × 2160) and must adapt cleanly down to 320 px phones without clipped content. Authority comes from strict alignment, mostly light surfaces, calibrated corporate blue and mint-teal contrast, functional indexing, and auditable numbers, not decoration.

`assets/template.html` already implements everything below. Start from it and change content, not the system. Read this file when you need to know *why* a rule exists or to extend the template with something new.

## Contents

1. Delivery and identity
2. Layout architecture
3. Colour
4. Typography
5. Components
6. Visualisation rules
7. Motion, accessibility, print
8. Adapting to the task

---

## 1. Delivery and identity

- **One self-contained file.** Every stylesheet, script, data block and font lives inside the HTML. The three WOFF2 faces (IBM Plex Sans 400, IBM Plex Sans 600, IBM Plex Mono 400) are embedded as base64 by `scripts/embed_fonts.py`, which also inserts the SIL Open Font License notice as a comment. Local fallbacks: `system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif` and `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`. The file opens and runs from disk with no server and no network.
- **Readable without JavaScript.** The initial HTML must already contain the baseline state: headline numbers, the conclusion, each figure's text account and its fallback table. Scripts enhance; they never create the only copy of a finding.
- **Light first.** The page always opens in the Light theme, whatever the device's dark-mode setting, so there is no `prefers-color-scheme` block. A visible Light/Dark switch sets `data-theme` on `<html>`. Switching theme must not reset sliders, presets or open disclosures (the template only swaps tokens, so state survives). Remembering the choice in `localStorage` is optional and must be wrapped in `try/catch`.
- **No attribution, no decoration.** No bylines, author credits, watermarks, stock photos, decorative illustrations, background scenery, glows, invented status telemetry ("System online ●"), or looping animation. The design grammar is the signature.

## 2. Layout architecture

| Element | Rule |
|---|---|
| Container | Centred, `max-width: 1720px` (range 1680–1760), outer gutters 24–32 px; on phones the gutter is `max(16px, env(safe-area-inset-*))`. |
| Application bar | One 48 px navy bar holding: workspace context (document type · short title), numbered section links (`01`, `02` …), an optional guided-sequence stepper, and the theme switch. Sticky at the top. No second header. |
| Hero | Unboxed, two columns on wide screens: oversized `h1` on the left; concise framing thesis plus 2–4 headline baseline metrics on the right. A 1 px structural divider below. |
| Panels | Rectangles with 20–24 px padding, 4–6 px radius, 1 px border, shadow `0 2px 12px rgba(0,49,104,0.09)` light / `0 2px 12px rgba(0,0,0,0.26)` dark. Grid gaps 16–24 px. |
| Editorial band | Prose column 65–72ch on the left, paired with a right analytical margin rail (`1fr`, roughly one third) for scope guardrails, definitions, chronologies or summary tables. Never stretch paragraphs across the full width and never leave the right two-thirds empty. |
| Figure | The unit of the page: a small 1:2 workbench (controls `1fr`, stage `2fr`) placed in the reading flow, directly after the paragraph that sets it up. One question, one or two controls, a reset, a live line. Stacks controls above the stage on narrow screens. |
| Sandbox | The closing workbench, after every control has been met alone: prose steps beside a sticky 1:2 workbench, usually in the one Midnight focal stage. |
| Focal stage | At most one Midnight (`#071B2C`) panel per document, for the hero simulation or primary result. Everything inside it uses Dark-theme tokens (the template's `.stage-dark` class does this). |
| Rhythm | Prose, figure, prose: each figure sits between the paragraph that frames its question and the paragraph that interprets it. Editorial bands with margin rails carry definitions and context. Never repeat identical rows of cards, and never stack figures without prose between them. Ordinary scrolling by default; a guided step-by-step mode only when asked for. |
| Detail | Methodology, formulas and audit tables go in `<details>` panels. On wide screens an opened disclosure is a two-column split: narrative assumptions left, tabular figures right. |

Breakpoints used by the template: 1180 px (rail and workbench go side by side), 760 px (hero splits into two columns), 520 px (metric grids go to two columns). Test at 320, 1440 and 2560 px.

## 3. Colour

Roughly 80% of a light page sits on Ice and White. Blue means baseline; teal/mint means active control or modelled scenario. Keep that pairing everywhere, including in charts.

| Token | Role | Light | Dark |
|---|---|---|---|
| `--canvas` | Page background | `#F3F7FB` Ice | `#071B2C` Midnight |
| `--panel` | Editorial, margin, control and chart panels | `#FFFFFF` | `#102A40` |
| `--bar` | Application bar | `#003168` BlueBay Navy | `#102A40` |
| `--bar-active-bg` / `--bar-active-fg` | Active theme button | `#F3F7FB` / `#003168` | `#F3F7FB` / `#003168` |
| `--focal` | Focal result stage | `#071B2C` Midnight | `#002750` Deep Navy |
| `--heading` | Titles, section and panel headings | `#003168` | `#F3F7FB` |
| `--text` | Body prose, table labels | `#344F68` | `#D5E1ED` |
| `--muted` | Captions, metadata, axis labels | `#53677A` | `#A3B7C7` |
| `--baseline` | Baseline series, current state | `#0051A5` RBC Blue | `#73B0E3` RBC Light Blue |
| `--accent` | Sliders, inline triggers, section codes, modelled series | `#007F73` Teal | `#35E0C1` Electric Mint |
| `--line` | Borders, dividers, light chart guides | `#D5E1EA` | `#365169` |
| `--segment` | Waiting segments, inactive capacity, secondary bands | `#CADBEA` | `#35546D` |
| `--control` | Recessed control groups and pills | `#EAF2F8` | `#18364E` |
| `--selected` | Selected pill / segment | `#003168` | `#73B0E3` |
| `--warn` | Risk threshold, amber | `#9A5B00` | `#F2B45A` |
| `--risk` | Incident or breach, crimson | `#B4232F` | `#FF8A8F` |

Contrast rules: Teal `#007F73` is for small accent text on white or ice. Electric Mint `#35E0C1` is only for dark surfaces or non-text marks. Warning colours always travel with a text label or a shape cue, never colour alone. Normal text at least 4.5:1, large text at least 3:1.

## 4. Typography

IBM Plex Sans for all headings and prose. IBM Plex Mono, always with `font-variant-numeric: tabular-nums`, for numeric readouts, units, axis scales, section index prefixes and compact labels. Never set body copy in mono and never decorate with fake coordinates.

| Element | Face | Size | Details |
|---|---|---|---|
| `h1` | Sans 600 | 44–56 px (36–42 px on phones) | Tracking −0.035 to −0.04em, line-height 1.08 |
| Section / panel headings | Sans 600 | 20–28 px | Section `h2` carries a mono accent index (`03`) and a 1 px bottom border |
| Reading prose | Sans 400 | 17–18 px | Line-height 1.6–1.65, 65–72ch |
| Controls and tables | Sans or Mono 400 | 12–14 px | Numeric columns right-aligned in Mono |
| Kickers, axes, metadata | Mono 400 | 11–13 px | Uppercase, tracking 0.035–0.055em |
| Key readouts | Mono 400 | 24–36 px | Tabular numerals, always with an explicit unit |

Headings use `text-wrap: balance`. One bold insight per key paragraph (prose rule 4) is the only routine use of bold in body text.

## 5. Components

All of these exist in the template; reuse them rather than inventing new ones.

- **Evidence tags** (`.tag-measured`, `.tag-modelled`, `.tag-assumed`, `.tag-illustrative`): small mono pills attached to numbers, figures and tables to show epistemic status. Every figure caption carries one.
- **Metric tile** (`.metric`): kicker, mono value with unit, one-line note, evidence tag. Use in the hero and compact metric grids only.
- **Margin rail cards** (`.rail-card`): definitions (`<dl>`), chronologies (`<ol class="timeline">`), scope guardrails ("In scope / Out of scope"), summary tables.
- **Inline trigger** (`button.trigger`): a phrase inside a sentence that sets the adjacent model to a named preset. Teal, dotted underline, 44 px hit area via padding.
- **Figure** (`figure.figure`): `.fig-controls` + `.fig-stage`, a reset, a `.live` line computed from the model, and a static `.fig-summary`.
- **Prediction prompt** (`.predict`): a question, two or three answer buttons, and an explanation revealed on answering (always visible without JavaScript and in print).
- **Sandbox workbench** (`.walkthrough` > `.workbench`): sticky controls and stage beside prose steps, readout tiles, the focal stage.
- **Disclosure** (`details.audit`): "Method and assumptions" with the two-column split.
- **Example viewer** (`figure.ex`): for product and API explainers, shows a real call and its real response, selected fields first with a "Show whole response" toggle and a copy button. Feed it from an embedded `<script type="application/json">` block.
- **Callouts**: `.callout` (neutral), `.callout.warn` (a condition that could flip the recommendation; never collapsed).
- **Outcome chips** (`.chip`): short mono labels for discrete states, coloured by meaning.

## 6. Visualisation rules

The interaction design itself is in `interactive-models.md`. Visual rules:

- **Truthful baseline on load.** A complete analytical view appears without interaction: explicit units, true scales, labelled illustrative data, and clear conceptual distinctions (elapsed turnaround time is not staff labour saved; say which a chart shows).
- **Widescreen density.** Do not stretch one sparse bar across 1,700 px. Build compound graphics: a trajectory with a threshold line and an uncertainty band, a queueing curve with its capacity line and annotated knee, a causal graph with the active path highlighted. Use sparse reference lines for simple comparisons and full axes for complex charts.
- **Direct annotation** beats legends: label the series at its end, label the threshold on the line.
- **Consistent scales** across compared panels. Distinguish the current value from historical traces; clear or recompute a trace when assumptions change.
- **Colour meaning is fixed:** blue baseline, teal/mint modelled, amber warning, crimson breach, segment grey for waiting or inactive.

## 7. Motion, accessibility, print

- Transitions of 200–350 ms only to clarify a data change; none under `prefers-reduced-motion`.
- 44 px minimum touch and click targets; visible focus outlines on every interactive element; `aria-live="polite"` on result readouts; native labelled inputs.
- **Print:** switch to Light tokens, hide the application bar and interactive controls, open every `<details>` before printing so calculations print in full, and restore the previous open/closed state afterwards (`beforeprint` / `afterprint`). Do not add a Print button; some viewers block `window.print()`.

## 8. Adapting to the task

Same identity, different emphasis:

| Document | Emphasis |
|---|---|
| Product or feature explainer | Persistent cast, capability sections ordered by dependency, example viewers with real outputs, worked cases, quick reference. |
| Topic explainer | Toy model first, then one small figure per model; definitions in the margin rail; boundary cases at section ends; sandbox last. |
| Strategy paper | Broad scenario model in the focal stage, options compared on the same scales, expandable evidence, "what would change our mind". |
| Incident explanation | Process or causal diagram with impact and recovery states, timeline in the margin rail, contributing factors versus root cause, actions with owners. |
| Funding request | Prominent option comparison, auditable assumptions table, sensitivity to the two or three inputs that matter, the ask stated in the hero. |
| Operational update | Compact progress views and metric grid, what changed since last time, risks with thresholds, decisions needed. |
