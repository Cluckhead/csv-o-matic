# Asking for what's missing

The skill asks multiple-choice questions only when the answer would change the page and cannot be worked out from the request, the source material or sensible defaults. Questions are generated from the material in hand, so their options are specific to this subject, not a fixed questionnaire.

## What the page needs

Check each item against what you already have. Mark it known, inferable (state your inference later) or missing.

| Item | Why it matters | Default if not asked |
|---|---|---|
| Subject and scope | What is in and out | From the request |
| Audience | Sets vocabulary, depth and what to assume known | Senior, non-specialist leaders |
| Outcome statement | What the reader will be able to predict, decide or explain afterwards; this decides the model and every figure | Derive from the request when it is clear; ask when several outcomes are plausible |
| Core thesis | The one-sentence conclusion the hero states | Derive from the material |
| Document type | Explainer, strategy, incident, funding, update | Infer from the material |
| Shape and length | Brief, explainer or field guide; any length the user wants | A stated length is the target; otherwise proportionate to the topic (`page-method.md` §2) |
| Ground truth | Real product, data or documents to draw on | Ask if none was given and the page makes factual claims |
| Data status | Measured, modelled, assumed or illustrative | Illustrative, labelled as such |
| Interactivity | Which mechanism deserves a working model | One model on the central mechanism |
| Sensitivities | Facts that must not appear, names to avoid | Invented names for all examples |

## When to ask

- **Ask** when a missing item is one the user alone can decide (the reader's job, the thesis when the material supports several, whether figures are real) or when a wrong guess would waste a long build (tier, document type).
- **Do not ask** about anything the material answers, anything with a safe default above, or matters of design and voice (the skill already defines those).
- **Ask once, together.** One round of up to four questions. A second round only if the answers open a genuinely new gap. If everything is known or inferable, ask nothing and say in one line what you assumed.
- For a short request with rich material ("make a one-pager from this incident report"), usually ask nothing.

## How to ask

Use the environment's multiple-choice tool when there is one (in Claude Code, `AskUserQuestion`). Otherwise ask inline as a short numbered list with lettered options and invite a one-line reply such as "1b, 2a".

Each question:

- **Stands alone.** The user may see only the question dialog, so name the subject inside the question.
- **Has 2–4 options drawn from the material**, each a concrete candidate, not a category. Put your recommendation first and mark it "(Recommended)".
- **Explains the consequence** of each option in its description: what the page will look like or emphasise.
- Leaves room for "Other" (the tool adds it automatically).

## Making options dynamic

Generic options waste the question. Derive them from what you read.

**Thesis.** Read the material, draft the two or three conclusions it could support, and offer those.

- Generic (bad): "What is the main message? a) Benefits b) Risks c) Overview"
- Dynamic (good): "The positions incident supports more than one headline. Which should lead? a) A lost leading zero in the custodian file silently dropped positions (Recommended) b) Positions are built before the security master's final run, so late corrections never reach clients c) Nothing in the chain checks that row counts reconcile"

**Outcome.** The most valuable question when the request is vague, because it decides the model. Offer the two or three outcomes the material supports, each as something the reader could do afterwards.

- "What should a steering committee member be able to do after reading the pricing-engine page? a) Predict which trades get repriced when a vendor sends a correction (Recommended) b) Decide between keeping the old engine and cutting over in March c) Explain to a client why their price changed overnight"

**Audience.** Offer the two or three groups the material implies, with what each needs.

- "Who is the guide for? a) Operations analysts who run the investigations: full tool detail and worked cases (Recommended) b) Engineering leads deciding whether to adopt it: architecture, limits and evidence it works c) Executives: one-page brief on what it does and the risk it removes"

**Shape and length.** Ask only when the user has not said and the material would support very different pages. Tie the options to the reader's job and the topic's size, and describe each by what it covers, not by a word count.

- "How long should the explainer on the new pricing engine be? a) Brief: one screen on the decision to switch, with one scenario model b) Explainer: how the engine prices, where it differs from today, three models (Recommended) c) Field guide: every rule and parameter, for the team that will maintain it"

**Data status.** Ask when numbers will appear and their provenance is unclear.

- "The deck has throughput figures for Q3. How should the page treat them? a) Measured: show as fact with their source (Recommended if they come from the ops dashboard) b) Directional: show rounded, tagged as estimates c) Replace with illustrative figures and say so"

**Interactivity.** Offer the two mechanisms that most deserve a model.

- "Which mechanism should readers be able to play with? a) Queue build-up when arrivals exceed capacity (Recommended) b) How batch size trades latency against cost c) Both, as two workbenches"

## After the answers

Restate the brief in three or four lines (audience, job, thesis, tier, data status) at the start of your work so the user can redirect early, then build without further check-ins unless something blocks you.
