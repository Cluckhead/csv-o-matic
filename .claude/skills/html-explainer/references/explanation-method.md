# Explanation method: building an argument out of small experiments

This is the heart of the skill. It distils how Bartosz Ciechanowski constructs interactive explanations, and how Josh Comeau composes and maintains them, into instructions. Read it before planning any page.

The short version: **a page is a readable argument with a few carefully chosen experiments.** A reader changes something, sees a consequence, and is told enough to interpret it. Quality comes from the fit between the question, the model, the action and the prose, not from the number of controls. The reader should finish able to explain a mechanism, predict a consequence or assess a proposal.

## Contents

1. Define what the reader should become able to do
2. Construct a sequence of questions
3. Keep the same objects; change one condition
4. Teach the visual language before relying on it
5. Write around an act of observation
6. Finish with a prediction the reader can make
7. Choose the interaction by the reader's question
8. Decide when an interaction has earned its space
9. Give exploration a useful starting point
10. Moves worth borrowing
11. Different purposes need different structures
12. What to adapt cautiously
13. What this is not

---

## 1. Define what the reader should become able to do

A topic is too broad to decide anything. "Explain the new pricing engine" leaves every design choice open. "Help the reader predict which trades the engine will reprice when a vendor sends a correction" specifies an observable outcome, and that outcome decides the model, the figures and the test of understanding.

Write the **outcome statement** before anything else, in this form:

> After this page, the reader can *[identify / predict / decide / explain]* … *[specific, checkable thing]*.

Examples:

- "After this page, the reader can identify which stage limits throughput and predict whether adding capacity elsewhere will help."
- "After this page, the reader can tell from one search result whether a security is absent, present, or not yet known, and say what to do next."
- "After this page, the reader can decide whether the cut-off change or the extra hire shortens month-end close, and say under what condition the answer flips."

If you cannot write it, you do not yet understand the subject well enough to explain it. Go back to the material.

## 2. Construct a sequence of questions

Bartosz's later essays are a chain of **increasingly capable models**. Each section answers one question and, in doing so, creates the conditions for the next. Complexity earns its place because the reader has just met a reason for it.

To plan the chain:

1. Start with the **simplest explanation** that answers the opening question: one request through one process, one activity on one timeline, one file read once.
2. Find where that explanation **gives a wrong prediction or leaves out** something the reader needs.
3. Introduce the next concept **precisely there**, and only that concept.
4. Repeat until the outcome statement is reachable.

Example: start with one request passing through one process. Add a second request to expose contention. Introduce a queue when simultaneous work cannot be handled. Then vary durations to show why averages mislead. Each step repairs a specific limitation in the reader's current model.

Order by **conceptual dependency**, never by organisation chart, project history or the order of a codebase. Ask of every section: what must someone understand before this claim is intelligible?

For executive readers the conclusion still comes first (see `prose-style.md`): the hero states the finding and why it matters, then the chain of models shows why it is true. Bartosz's long build-up rewards sustained attention; a busy reader first needs to know why that attention is worth spending.

## 3. Keep the same objects; change one condition

When every figure has a new layout, new names and new colours, the reader relearns how to read each one. Instead, **keep the same objects across figures and change only the condition under discussion.** The reader then sees the same request, the same activity, the same file, behaving differently, and the explanation can concentrate on why.

- Define the cast before writing (see `page-method.md`): the entities, their identifiers, and the one trait each exhibits.
- Keep each entity's position, colour, label and identifier stable from figure to figure.
- Give each meaning one encoding for the whole page: if hatched grey means "waiting", it means waiting everywhere.
- Pair colour with shape or text so meaning survives greyscale printing and different colour perception.
- Introduce parameters **one at a time** (Josh's spring-physics ordering): a figure that varies documentation only, then one that varies build only, then a sandbox that lets both vary. Giving every control equal prominence at the start invites the reader to change three things and learn nothing.

## 4. Teach the visual language before relying on it

An attractive picture is not self-explanatory. Bartosz's *Airfoil* spends real effort teaching how to read flow, velocity and pressure before using them to explain lift.

- The first figure on the page introduces the encoding with the simplest possible case and says, in prose, what each mark means: "Each bar is an activity; its length is its duration in working days. The diamond is launch. Hatching is time an activity sits finished, waiting for something else."
- **Define what each mark measures** before asking for interpretation. Distinguish instantaneous values, rolling averages and end-of-period totals; say which a chart shows.
- When two representations use **different time assumptions** (a live simulation beside a settled summary curve), say so in the prose.
- When a figure shows the **author's complete view** but an actor in the system only sees part of it (a receiver, a monitoring system, a model calling a tool), make the distinction visible: grey out what the actor cannot see, or show the two views side by side.

## 5. Write around an act of observation

"Add explanatory text" is too weak an instruction. Every interactive figure must have an explicit relationship with the prose around it. A productive passage has this sequence, written naturally rather than as labels:

1. **Establish the question.**
2. **Define the representation** (only the parts that are new).
3. **Invite one action.**
4. **Direct attention** to what will be observed.
5. **Explain its significance**, including the trade-off.
6. **Qualify** where the model would otherwise imply too much.

An example, for a service that sends completed work in batches:

> "The service sends completed work every five minutes. A request that finishes just after a dispatch waits almost the full interval, even though its processing is complete. Move the completion marker across the next dispatch time. Notice that a small change in completion time produces a large change in waiting time. Shorter dispatch intervals reduce this source of delay, although they may create more frequent dispatches."

The figure makes the discontinuity inspectable. The prose supplies the distinction between processing and waiting, says where to look, and states the trade-off. Without the writing, the reader drags the marker without learning anything.

**Prose must remain true after interaction.** The reader can invalidate a sentence like "the queue is now shrinking" by moving a slider. So either anchor the sentence to a named case ("In the *baseline* case…"), use conditional wording that holds across the whole range ("Whenever arrivals exceed capacity, the backlog grows"), or put a short **live observation** beside the figure that is computed from the same model result as the drawing. Never generate a second, independent interpretation that could contradict the figure.

**Use prose for what pictures cannot do**: definitions, why a pattern matters, which interpretation to reject, whether the relationship is measured or modelled, and where the result stops applying. Do not narrate what the reader can already see.

**The argument must survive a quick reading.** State every conclusion in text beside an interpretable default state. Interaction lets the reader inspect more deeply or test the conditions; it never holds the only copy of the finding.

## 6. Finish with a prediction the reader can make

The most useful outcome is a mental model that survives the page. A reader who can reproduce the author's slider setting has learned less than one who can predict an unfamiliar setting.

- After a demonstration, **ask what would happen under a nearby change**: a different arrival pattern, the bottleneck moving, a delayed measurement. The answer should follow from the mechanism already introduced.
- Use the template's **prediction prompt** (`.predict`) at least once on any page longer than a brief: the reader commits to an answer *before* the figure shows it, then the figure moves to that case and the explanation appears. Choose a case where the naive answer is wrong.
- End major sections with a **boundary case**, a configuration where the rule just taught changes. In the deadline example, documentation becomes the controlling branch once it exceeds 20 days; without that last experiment the reader may conclude documentation never matters.
- Use the prediction as your own review criterion: can a reader who has finished the page answer an unfamiliar case correctly? That matters more than the number of charts.

## 7. Choose the interaction by the reader's question

The unit of design is **a reader question paired with an operation that helps answer it.** Choose by purpose, from this vocabulary:

| Reader question | Interaction | What it makes clear | Design condition |
|---|---|---|---|
| What changes when this input changes? | Parameter slider or numeric input | Direction, sensitivity, thresholds | Show units and a meaningful range; say what stays fixed |
| What happens in what order? | Time scrubber or event stepper | Sequence, intermediate states, timing | Distinguish elapsed time from a step number |
| What is hidden inside this object? | Cutaway, layer toggle, isolation | Internal structure, dependencies | Preserve orientation; offer a reset |
| Is this a change in the system or in my view? | Camera or reference-frame control | Different observations of one state | Label view controls apart from model controls |
| Which version behaves differently? | Linked comparison frame | Effect of a single changed assumption | Hold inputs and scales constant across panels |
| Where does this result come from? | Linked mechanism, chart and calculation | How a metric arises | Derive every view from the same state |
| Why does the current approach fail? | Failure trigger or boundary case | Limits of a design | Show a plausible failure and its conditions |
| What example is the author describing? | Named preset triggered from prose | A reproducible case | Update every dependent control and output |
| What is possible within these rules? | Direct manipulation | Constraints, reachable outcomes | Make legal moves discoverable; offer non-drag controls |
| Which component matters here? | Highlight, isolate, mute | Contribution and structure | Keep enough context to explain the relationship |
| What happens when effects combine? | Staged construction or contribution toggles | Composition and interaction | Introduce components before all combinations |
| Does the rule survive other cases? | Bounded sandbox | Generalisation | Useful default, reset, and a question to investigate |

These impose different reasoning. Moving a camera inspects a representation; changing an input explores a model; filtering historical data changes the evidence. **Identify the type of claim before choosing the control.** A region filter on a sales chart does not show what would happen if the company changed its regional strategy; only a scenario model with stated assumptions could, and filtering cannot supply those assumptions.

## 8. Decide when an interaction has earned its space

Before building a figure, write what the reader should notice. Then decide:

- If the answer is "a number changes", a small table may be clearer.
- If a stable relationship must be compared precisely, a static chart may be better.
- If motion exposes **ordering**, or a change of **geometry**, or a **threshold** the reader would not believe from a table, interaction has a strong case.

Interaction costs attention, adds states that must all be correct, and slows a quick reading. **You may reject a planned interaction** once you find its benefit is small, and say so in the plan. A page with three figures that each teach something beats a page with ten that each move.

Sometimes **removing a degree of freedom** teaches more than adding one. *Curves and Surfaces* locks two parameters together and the resulting constrained path reveals the relationship. Likewise, a figure where only the critical-path activity can be changed may teach more than one where everything can.

## 9. Give exploration a useful starting point

An empty sandbox is a poor opening: the reader does not know which control matters or what a meaningful result looks like.

- **Start in a state where the important relationship is already visible**, then direct one small experiment.
- Defaults, ranges and step sizes are part of the explanation. A threshold that falls between two coarse steps becomes invisible; a range chosen so every scenario looks favourable hides the sensitivity the reader should see.
- **Named cases** ("Normal load", "Short interruption", "Prolonged interruption") give interpretable starting points, provided their values are stated and their choice justified. Free adjustment comes after.
- **Every figure has a reset** to an explicit baseline. Presets set every relevant value, never "whatever the reader left selected".
- For random simulations, **keep the same generated inputs** when comparing designs, or one design looks better because it drew an easier workload. Make fresh randomness a deliberate action and distinguish a single run from an aggregate.

## 10. Moves worth borrowing

Techniques from specific articles, stated so they transfer to business and product subjects.

- **Make complexity necessary** (*GPS*). Let each design requirement emerge as the answer to a problem the reader has just seen. An architecture is easier to assess when each component answers a demonstrated need.
- **Prose links that set the figure** (*GPS*). Phrases in the text set the adjacent figure to the case being discussed, so the reader never hunts for the right combination of controls. Implemented as `button.trigger`.
- **Author's view versus actor's view** (*GPS*). Separate what the explanatory scene shows from what a participant can actually know. Essential for monitoring, permissions, and anything a model or service decides with partial information.
- **Teach the encoding first** (*Airfoil*). See section 4.
- **Constraint as insight** (*Curves and Surfaces*). Lock parameters together to reveal a relationship; choose a view that **amplifies** the property under discussion (reflections exposed surface continuity; recovery behaviour after the same failure can expose two designs that look identical as flowcharts).
- **Mechanism beside its measurement** (*Gears*). Show the moving thing and the quantity it produces in one view, driven by one control: a process beside its growing queue, a timeline beside its accumulated cost. The shared state matters more than a second chart.
- **Explain each part by its function** (*Mechanical Watch*). Introduce the whole, then each subsystem as the answer to "why is this here?" A component inventory alone does not produce understanding.
- **Reconnect to scale** (*Mechanical Watch*). After enlarged views, show a real-size reference. An enlarged exception path may deserve attention without representing most of the workload; say what share of the real volume it is.
- **Hold the right things equal** (*Bicycle*). When comparing designs, identify what must stay equal for the comparison to isolate the claimed cause, and hold it equal visibly.
- **Static versus disturbed** (*Naval Architecture*). A design that looks adequate at rest may fail under disturbance; show both.
- **Operate the real thing** (Josh's *Flexbox*). When explaining a feature, let the reader operate a small instance of the actual behaviour, or real outputs captured from it, instead of an animation that might teach behaviour the product does not have. Keep the surrounding environment simple so the reader knows which condition caused the change.
- **Concept-level controls** (Josh's *Shadows*). One reader-facing control ("elevation", "load") can drive several implementation values through an explicit, testable mapping. Expose individual values only when their separate effects are the lesson.
- **Say when a parameter can be ignored** (Josh's *SVG paths*). Acknowledge a capability without making every reader master it.
- **A long piece can prepare the reader for a tool** (*Exposing Floating Point*). Not every example needs to be an embedded simulation; sometimes the page should hand off to the real tool.

## 11. Different purposes need different structures

Classify the task first; the structure follows.

| Task | Organising question | Promising interaction | Essential written content |
|---|---|---|---|
| Explain a feature | What happens when someone uses it under different conditions? | A small real or explicitly simulated instance with a few named cases | Intended use, prerequisites, limits, expected outcome |
| Explain a problem | Which mechanism produces the symptom? | Trace, failure trigger, controlled comparison | Evidence, competing explanations, what would distinguish them |
| Explain a proposal | Under what assumptions does this option help? | Baseline and proposed case with shared inputs | Alternatives, trade-offs, uncertainties, the decision required |
| Explain a project | What determines delivery or impact? | Dependency view with a constrained scenario model | Scope, dependencies, ownership, assumptions, implications |
| Explain observed data | What pattern is present, and how stable is it? | Linked filtering, comparison, detail inspection | Population, measurement definition, period, missing data, limits on causal reading |

A demonstration of product behaviour is not evidence of business impact. An attractive scenario is not a forecast. A correlation in observed data is not a mechanism.

## 12. What to adapt cautiously

- **Length.** The length of Bartosz's later essays is not a target, and neither is any fixed word count. The topic sets the length: one step per idea the reader needs, no padding, no missing steps. A length the user states is the target. State what the reader will be able to do, give a short route through the central argument, and let further detail earn its place.
- **A model can make an assumption look inevitable.** Physical systems have firm relationships; business systems involve behaviour, incentives and incomplete evidence. If productivity is *assumed* to rise with adoption, a slider showing that has only illustrated the assumption. Show the source of each relationship, allow alternative assumptions where they are plausible, and say what result would change the recommendation. Where the mechanism is disputed, compare plausible models side by side. Where evidence is too weak to choose parameters, use a conceptual diagram instead of a simulation.
- **Interaction enlarges the editorial surface.** A static figure has one state; an interactive one may have hundreds, some contradicting the prose or producing nonsense. Review minimum and maximum values, the teaching cases and the boundaries where the conclusion changes. Restrict impossible combinations openly and say why; silent clamping makes it look as though the model used a value it did not.
- **Reuse must not decide the argument.** If every page becomes the same cards and a sensitivity slider, the template is writing the explanation. Each figure needs a reason for its representation, and bespoke components are allowed where the mechanism warrants them.

## 13. What this is not

- Not a dashboard. Readout tiles and a bank of sliders are not an explanation.
- Not a reference manual with JSON attached. Reference material (parameter tables, raw responses) supports the argument; it does not replace it. Put it beside or after the experiment that makes it meaningful.
- Not an imitation of anyone's typography, colour or motifs. The identity comes from editorial commitments: begin with the practical question, establish one concrete case, expose the governing mechanism, let the reader test a boundary, and finish with a decision or implication. The visual system is the user's own (`visual-system.md`).
