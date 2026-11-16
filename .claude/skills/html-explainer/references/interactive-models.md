# Building the figures

How to implement the experiments planned in `planning.md`. The template (`assets/template.html`) contains a working implementation of everything here; copy its patterns rather than inventing new ones.

## Contents

1. Separate the parts
2. One source of truth
3. The component catalogue
4. The `Demo` API
5. Choose a renderer for the relationship
6. Diagrams from meaningful elements
7. Linked representations and comparisons
8. Controls
9. Dragging
10. Time: simulation versus animation
11. Make a long page cheap to read
12. Accessibility and alternatives
13. Verification

---

## 1. Separate the parts

| Part | Responsibility | What goes wrong if it is mixed with everything else |
|---|---|---|
| Article | Argument, definitions, sources, interpretation | The writing cannot be reviewed apart from the UI |
| Data and assumptions | Inputs, provenance, units, valid ranges | Illustrative numbers become indistinguishable from evidence |
| Model | Calculate outcomes or apply transition rules | Different figures quietly use different versions of the relationship |
| Scenario state | Inputs, current event, named case | Presets update only some of the relevant values |
| View state | Selection, zoom, highlighted path, visible layers | Changing the view changes the scenario |
| Renderer | Draw the model's current result | Decorative motion changes the apparent meaning |
| Controls and accessibility | Input, focus, labels, reset, alternatives | Every figure reinvents interaction and repeats its defects |

The reusable framework holds repeated explanatory operations (controls, presets, reset, stepping, comparison, prediction). The **domain model stays specific to the subject**: a capacity model and a permissions model may both need a stepper, but they share no rules. This separation also lets a reviewer check the assumed relationship before being persuaded by its presentation.

## 2. One source of truth

- Write the model as a **pure function**: parameters in, result out, no DOM. Every view (diagram, chart, readout, live sentence) consumes the same result.
- Prefer **one model for the whole page** with figures differing only in which parameters they expose. That is what keeps the same objects on screen from figure to figure.
- Do not store what can be derived. Storing both a queue and a separately editable total of queued items invites contradiction.
- Every interaction runs one sequence: **update state → calculate → redraw affected views → update labels and live region**.
- The live observation beside a figure is computed from the same result as the drawing. Never write a second interpretation that could disagree with the picture.
- Named presets set **every** relevant value; reset restores an explicit baseline.
- For random models, keep the same generated inputs when comparing designs; make fresh randomness a deliberate action.

## 3. The component catalogue

Start with this small catalogue and add a primitive only after several real pages need it.

| Component | Use it for | In the template |
|---|---|---|
| **Figure** | One question, one or two controls, a stage, a live line, a static summary | `figure.figure` with `.fig-controls` and `.fig-stage` |
| **Parameter control** | A labelled input with units and current value | `params` of type `range`, `choice` (button group) or `toggle` (switch) |
| **Named preset** | A reproducible teaching case | `presets`; `hidden: true` for ones reached only from prose |
| **Prose trigger** | Setting a figure to the case the text is discussing | `<button class="trigger" data-demo="name" data-preset="key">` |
| **Reset** | Returning to the explicit baseline | Added automatically to every figure with a presets bar |
| **Event stepper** | What happens in what order | `time: { min, max, step, events: [...] }` gives scrubber, play, previous and next event |
| **Comparison frame** | One changed assumption against a fixed baseline, same scale | Render two blocks from `BASELINE` and the current result (figure 4 in the template) |
| **Ghost baseline** | The original state shown behind the current one | Dashed outlines drawn from the baseline result (the template's sandbox) |
| **Direct manipulation** | Dragging the quantity itself | `bindBarDrag`: a handle with keyboard and slider alternatives |
| **Prediction prompt** | Committing to an answer before seeing it | `div.predict` with `data-demo`, `data-preset`, `data-answer`, `[data-choice]` buttons and a `[data-reveal]` answer |
| **Sandbox** | Combining everything already introduced | The one `.walkthrough` with a sticky `.workbench` and the Midnight `.stage-dark` |
| **Example viewer** | Real captured inputs and outputs as evidence | `figure.ex` fed from `<script type="application/json" id="examples">` |

## 4. The `Demo` API

```js
Demo.create({
  name: "docs",                         // used by prose triggers and prediction prompts
  root: "#docs",                        // the figure element
  params: {                             // only the parameters this figure is about
    docs:  { label: "Documentation", unit: "days", min: 1, max: 28, step: 1, value: 10, evidence: "illustrative" },
    mode:  { type: "choice", label: "Calendar", value: "work", options: [{ value: "work", label: "Working days" }, { value: "cal", label: "Calendar days" }] },
    cover: { type: "toggle", label: "Controller cover on close days", value: false },
    cut:   { label: "Cut-off", min: 9, max: 18, step: 1, value: 12, format: (v) => v + ":00" }   // format() customises the readout
  },
  presets: { base: { label: "As estimated", params: { docs: 10 } }, half: { label: "Halve it", params: { docs: 5 } },
             edge: { hidden: true, params: { docs: 22 } } },
  initialPreset: "base",
  view: { selected: null },             // view state, changed with api.setView()
  time: { label: "Day", min: 0, max: 20, step: 0.5, start: 20, rate: 4, events: [0, 4, 10, 20], format: (t) => "Day " + t },
  model: planModel,                      // pure
  render(result, state, ctx) { ... },    // draws every view from result; ctx.width, ctx.root, ctx.api
  describe(result, state) { return "..." } // the live line; state.preset names the active case
});
```

`params: {}` is valid: a stepper figure may have no model inputs at all. `api.set`, `api.applyPreset`, `api.setView`, `api.setTime` and `api.reset` all route through the same update sequence.

## 5. Choose a renderer for the relationship

- **HTML and SVG** first, for diagrams with identifiable objects, connectors and labels: process diagrams, timelines, dependency graphs. Each object stays addressable and can carry labels and focus.
- A **charting approach** (hand-built SVG axes, or a library loaded as a pinned UMD build) for conventional statistical plots where axes and comparison are central.
- **Canvas** when many marks or frequent redraws make retained elements awkward (thousands of particles, dense heat maps).
- **WebGL** only when spatial geometry, depth or occlusion carries the lesson. Most approval, dependency and scheduling relationships are two-dimensional; a rotating process diagram is harder to read and teaches nothing extra.
- None of these supplies an accessible explanation by itself (section 12). Choose the least complicated renderer that preserves the relationship.

## 6. Diagrams from meaningful elements

- Represent each logical object as a group whose shape, label and annotations move together, with a stable `data-id` so focus and selection survive redraws.
- Compute positions from data through explicit scales: start sets x, duration sets width; dependencies set connections.
- Layer background structure, active objects and annotations separately.
- Give each meaning one encoding for the whole page, and pair colour with a shape, pattern or text label (the template's spare time is hatched *and* labelled "10 days spare").
- Show the author's view and an actor's limited view separately when the lesson depends on what a participant can know.

## 7. Linked representations and comparisons

- Pair a mechanism with its measurement in one figure, driven by one control: the process beside its growing queue, the timeline beside the launch date.
- Selecting an item highlights its counterpart in every view; scrubbing time moves every view to the same instant.
- **Comparison frames hold scales and inputs constant** except the one assumption being compared. Draw both from the same renderer so encoding cannot drift.
- Distinguish current values from historical or baseline traces (the template uses dashed outlines). When an assumption changes, recompute or clear a trace rather than leave one that implies something false.
- When two views use different time assumptions (live versus settled), label the difference.

## 8. Controls

- Native labelled controls: `input[type=range]`, buttons, button groups, switches. Units and the current value sit beside the label.
- Ranges and steps expose the important transition. A threshold that falls between steps becomes invisible; a range in which every case looks good hides the sensitivity.
- One role per control: a time scrubber selects a moment; a parameter changes an assumption; a layer toggle changes visibility. Label view controls apart from model controls.
- Introduce controls **one at a time across figures**; combine them only in the sandbox.
- **Concept-level controls**: one reader-facing control may drive several implementation values through an explicit, testable mapping.
- Restrict impossible combinations openly and say why a control is unavailable. Silent clamping makes it look as though the model used a value it did not.

## 9. Dragging

- A visible handle with a hit area of at least 44 px on screen (scale the SVG hit rectangle by the on-screen ratio).
- Pointer Events with capture bound to a container that survives redraws, so a drag continues even though the figure is rebuilt.
- Convert pointer positions through the SVG's inverse screen matrix (`getScreenCTM().inverse()`).
- Constrain movement to valid values, and offer a keyboard path on the focused handle plus the equivalent slider. Touch users need an alternative to precise dragging, not just keyboard users (WCAG 2.2 "Dragging Movements").
- `touch-action: none` on the handle only, so the page still scrolls elsewhere.

## 10. Time: simulation versus animation

There are two reasons for a picture to move: the subject changes over time, or the interface is transitioning between states. Keep them apart.

- Say what motion represents: time, sequence, or only visual orientation. A marker gliding along a dependency arrow implies travel time where "requires" was meant.
- Do not let a presentational transition overshoot or interpolate through values the model never produced.
- `requestAnimationFrame` with progress from elapsed time, never a fixed step per frame.
- Playback speed is independent of the modelled process's speed; a numerical integration step is chosen for accuracy, not frame rate.
- Play, pause, previous and next event, and scrubbing that computes state directly from time, so any moment is reproducible.
- Never start a second loop on a second click; pause when the figure leaves the screen.
- Under `prefers-reduced-motion`, arrive at the final state immediately and rely on stepping.

## 11. Make a long page cheap to read

- Initialise heavy figures near the point of use; suspend off-screen work (`IntersectionObserver`).
- Recalculate only when an input changes; redraw only when something changed.
- Reserve each figure's space so the text does not jump while it loads.
- If a figure fails, its default example and text explanation remain (the static `.fig-summary`).
- Print and export use a meaningful state with the selected assumptions visible.

## 12. Accessibility and alternatives

- Labels, units, visible values and keyboard operation for every control.
- Announce the settled result, not every frame: the `.live` line is `aria-live="polite"` and changes only when the result does.
- Every figure's conclusion exists in text without the figure. Readers who cannot see or operate it lose nothing essential.
- Pause and event stepping wherever time matters; reduced motion honoured for non-essential movement (WCAG 2.2 "Animation from Interactions").
- A useful static or stepped representation wherever continuous motion would otherwise carry the explanation.

## 13. Verification

- **Behaviour**: each control changes the intended input; every linked view agrees; presets reproduce their case; reset restores the baseline; minimum, maximum and threshold settings produce meaningful output. `scripts/check_page.js` covers most of this, including prediction prompts and the theme switch.
- **Calculation**: run the model on the teaching cases and boundaries in a quick script; compare with every number in the prose, hero, live lines and fallback text. Test any invariant the explanation relies on (launch equals the later branch finish, for instance).
- **Truthfulness across states**: walk the extremes of every control and read the prose beside it. Any sentence that becomes false needs a named-case anchor or conditional wording.
- **Understanding**: see `planning.md` §5. A figure that works perfectly can still teach the wrong thing.
