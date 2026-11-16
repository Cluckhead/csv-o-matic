# Prose style: progressive complexity for executive explainers

Read this before writing any copy. It covers the voice, the structure of the argument, sentence mechanics, and how to stay honest about evidence. The user wrote sections 2–5; keep to them closely.

## Contents

1. The voice
2. Win the first two minutes
3. Build progressive complexity by conceptual dependency
4. Engineer sentence rhythm for continuous momentum
5. Bridge observation and epistemic honesty
6. British English
7. Before and after
8. Final read-through

---

## 1. The voice

Write like a clever, well-read colleague explaining something over coffee: someone who has read all the documents so the reader doesn't have to, finds the mechanism genuinely interesting, and is honest about what is still uncertain. The two reference points are Matt Levine (Bloomberg, *Money Stuff*) and Ethan Mollick (*One Useful Thing*). Borrow their habits, not their phrases. Never claim to be them or quote their stock lines.

### From Matt Levine: make the mechanism funny because it is true

- **Build a toy model.** Shrink the system to the smallest case that still has the problem: "Imagine a bank with one depositor and one loan." Then add one complication at a time. This is the same move as section 3 below, so it should feel natural.
- **Explain the plumbing, plainly.** Say exactly who does what to whom and in what order: "The custodian sends a file. The file has a column called SecurityId. The join looks that up in the master. If it isn't there, the position quietly disappears." Concrete actors, concrete verbs.
- **Deadpan understatement.** Let an absurd fact sit in a flat sentence and do its own work: "The fix took four minutes. Finding it took eleven days." Do not wink or add "ironically".
- **Rhetorical self-questions.** Anticipate the reader's objection and answer it: "Why not just rerun the job? You can, and it works, and then it breaks again on Thursday."
- **The long sentence, then the short one.** A patient multi-clause explanation followed by a four-word verdict. This is the rhythm the user asks for in section 4.
- **Asides for colour, never for substance.** A parenthetical joke or historical footnote is fine. A condition that could reverse the recommendation is not an aside; it goes in the main text (section 2).

### From Ethan Mollick: practical, first-hand, honest about the frontier

- **Show the thing working.** Prefer a real example, a real prompt, a real output, a real number from a run over a description of capability. "Here is what came back" beats "the system can".
- **Name the frameworks.** Give recurring ideas short, memorable names the reader can reuse in a meeting ("the join trap", "the second-delivery problem"), and use exactly that name every time after.
- **Optimistic realism.** Be clear about what works now and what doesn't yet, without hype or doom. Say where evidence is thin.
- **Write to someone who will act.** End sections with what to do, check or decide, not with a summary of what was said.
- **First person, sparingly.** "I'd start with the process map" is fine for advice. Do not invent personal anecdotes or experiences; the page has no byline and the user has not lived them.

### Calibrating humour for executives

One dry line per section is plenty, and many sections need none. Never joke about incidents' impact on customers or colleagues, about named people, or in a section that states a risk or a recommendation. The test: would the line still be worth reading if it were not funny? If not, cut it.

### Tics to avoid

These make text read as generated or as filler:

- Openers such as "In today's fast-paced world", "Let's dive in", "It's worth noting that", "Importantly,".
- Inflated vocabulary: delve, leverage (as a verb), robust, seamless, unlock, game-changer, tapestry, landscape.
- Reflexive triplets ("fast, reliable and scalable") where one precise word would do.
- Stacked hedges ("may potentially help to some extent").
- Restating the heading in the first sentence.
- Ending sections with "In summary" or a recap of the paragraph above.

---

## 2. Win the first two minutes

Senior leaders rarely read from first principles out of idle curiosity; they typically give an explainer about two minutes to prove it deserves their attention. Yet complex operational topics cannot be reduced to superficial slogans without hiding critical risks. Every explainer therefore runs on two tracks at once: an **executive skim layer** that states the core conclusion in plain text without requiring any interaction, and a **progressively deeper causal narrative** that rewards sustained reading. Write in crisp, concrete British English so every paragraph earns the next minute of focus.

Recreational essays can afford a slow build-up; corporate readers need to know why a mechanism matters before committing their attention. Open directly with the principal finding, operational tension, or practical decision at stake. Instead of starting with background definitions, hook the reader with a concrete friction point, such as why speeding up a workstream leaves a launch date unchanged, or where an apparently stable process hides a sudden delay.

Structure the page spine so a time-pressed leader scanning before a meeting still captures the full argument:

- **Front-load section takeaways.** State each section's conclusion in its opening lines before unpacking how the mechanism produces it.
- **Write question-led headings.** Headings name substantive questions ("Why does the second delivery never reach positions?"), not topic labels like *Overview* or *Background*.
- **Keep deal-breakers visible.** Tuck technical derivations into secondary sections or `<details>` so they do not interrupt the main reasoning, but never hide a condition that could reverse the recommendation inside a footnote or collapsed panel.

Practical test for the skim layer: read only the hero, the section headings, the first sentence of each section and the bold sentences. That alone must carry the full argument and the recommendation.

---

## 3. Build progressive complexity by conceptual dependency

Once the opening secures interest, make complexity earn its place. Do not make readers reconstruct an organisation chart or project history; order sections strictly by conceptual dependency, asking what someone must grasp first before the next claim becomes intelligible.

Build the body as a causal chain of increasingly capable models:

1. **Start with the simplest viable case.** Track one concrete entity, such as a single request passing through one process, to establish baseline behaviour cleanly.
2. **Pinpoint where the simple model breaks.** Identify the exact moment the baseline account gives a wrong prediction or omits a vital constraint.
3. **Introduce the next layer to repair that flaw.** Add a second request to expose contention, introduce a queue when simultaneous work stalls, vary durations to show why averages mislead.

**Persistent identities.** When following a specific entity, keep its exact identifier and name consistent across prose, diagram, event log, tables and outcome summary. Changing labels mid-article forces readers to spend working memory relearning the setup instead of noticing why behaviour shifted. In practice: decide the cast (see `page-method.md`) before writing, and never rename a member of it.

---

## 4. Engineer sentence rhythm for continuous momentum

Executives often lose focus not because the subject is hard but because flat, repetitive sentence mechanics cause attention fatigue. Four rules:

- **Break metronomic cadence.** Avoid stacking paragraphs of uniform length and identical subject–verb–object syntax. Alternate longer, multi-clause explanatory sentences with crisp, decisive punchlines.
- **Supply explicit logical glue.** Do not place "island assertions" side by side and make tired readers infer how claim B relates to claim A. Use causal signposts: *because*, *consequently*, *in practice*, *by contrast*, *which means*.
- **Replace nominalisations with active verbs.** Strip abstract meta-nouns such as *representation, condition, operation, property, utilisation*. Put concrete actors and verbs on the page. Not "Adjustments to batch intervals alter latency outcomes" but "Halving the dispatch interval cuts idle waiting time."
- **Create visual landmarks.** Bold one core insight in each key paragraph, and turn multi-part criteria into compact lists. Balance setup against sharp takeaways so not every sentence reads like a dense proverb.

---

## 5. Bridge observation and epistemic honesty

For the passages directly around a figure, follow `explanation-method.md` §5: question, new encoding, one action, what to notice, why, and the limit.

Do not pad length by narrating patterns the reader can already see on screen. Write tightly around an act of observation:

1. frame the operational question;
2. define what each metric measures;
3. invite one targeted action ("Drag capacity down to 40 an hour");
4. highlight the resulting discontinuity;
5. state the practical trade-off.

Keep prose valid after the reader interacts. Anchor claims to named presets ("In the *Baseline* preset…") or write conditional rules that hold across the whole control range ("Whenever arrivals exceed capacity, the backlog grows"). Never write "the backlog is 120" next to a slider that can change it.

Protect credibility through strict epistemic discipline. Polished prose beside a smooth interactive model can make fragile business assumptions feel as settled as physics. So:

- **Label every number's status.** Separate *measured* historical data, *modelled* outputs of a formula, *assumed* inputs nobody has verified, and *illustrative* invented figures. The page uses visible tags for this (see `visual-system.md`).
- **Explain which rival interpretations fail**, and why.
- **State what would flip the recommendation**: the specific shift in an assumption, with its threshold if you can compute it.
- **Close major sections with a nearby boundary case**: "What happens if the second delivery arrives after positions have already been sent to the client?" This leaves the reader with a model that survives after the page is closed.
- **Say what is inferred.** If a link in the chain is reasoned rather than observed, say so in a sentence.

---

## 6. British English

Use British spelling and conventions throughout:

- *-ise* and *-isation*: organise, prioritise, optimisation, recognise.
- *-our*: colour, behaviour, favour. *-re*: centre, metre (unit). *-ce* noun / *-se* verb: licence/license, practice/practise.
- Doubled *l*: modelled, cancelled, travelled, labelled.
- *programme* (plan) but *program* (software). *analyse*, *catalogue*, *grey*, *judgement*, *whilst* only sparingly (prefer *while*).
- Dates as 13 November 2026; times in 24-hour form (21:15). Currency with the symbol first (£4.2m, €180k).
- Single quotation marks for quoted speech in prose are acceptable, but be consistent within a page.

---

## 7. Before and after

**Opening.**
- Before: "This document provides an overview of the reconciliation process and the recent issues identified within it."
- After: "On 13 November, 900 units of Halvorsen Aerospace vanished from a client report. Nothing failed. A leading zero went missing upstream, and every step downstream did exactly what it was told."

**Nominalisation.**
- Before: "The utilisation of additional capacity did not result in an improvement in completion timing."
- After: "Adding a second analyst did not move the launch date, because the work was waiting for sign-off, not for analysts."

**Island assertions.**
- Before: "The vendor sent a second file. Positions ran at 20:00. The master was rebuilt at 21:15."
- After: "The vendor sent a corrected file at 20:47, which means the master rebuilt at 21:15 was right. Positions, however, had already run at 20:00 against the old master, so the client saw the old price."

**Narrating the chart.**
- Before: "As you can see in the chart, the line goes up and then comes down."
- After: "Drag arrivals past 60 an hour. The backlog stops draining overnight, and from that point every extra request adds a full day's wait for someone."

---

## 8. Final read-through

Before delivering, read the page once as the hurried executive and once as the sceptical expert.

- Does the hero alone state the conclusion and why it matters?
- Do the headings, read in order, tell the story as questions and answers?
- Is every number tagged measured, modelled, assumed or illustrative?
- Is any deal-breaker hidden in a collapsed panel? Move it out.
- Is every entity named identically everywhere?
- Is there a sentence anywhere that would become false if the reader moved a slider?
- Does each major section end with an action, a decision or a boundary case rather than a recap?
