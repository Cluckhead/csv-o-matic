# CSV-o-Matic Field Guide: plan

Plan for `docs/field-guide.html`, built with the `html-explainer` skill. It records the reasoning, the model and
the evidence behind the page so they can be checked apart from the presentation.

## Reader and outcome

- **Reader:** operations and data analysts, the engineers who run the ETL, and leads deciding whether to trust the
  server's answers. They know CSV files and their own pipelines; they do not know the server's rules.
- **Question they arrive with:** "If the model says a security is missing from a file, is it?"
- **After reading, they can:** read any CSV-o-Matic response and say whether it means found, absent or not yet
  known; predict which of the three a given question will get; and name the call that should come next.
- **Unfamiliar case to test that** (never shown directly): `search_across_columns` for `417382` in a golden
  security master stops at the time limit having read most of the file with no match. Later it completes with no
  match and a near miss, one value containing the text inside a longer value. *Answer:* the first response is
  `inconclusive`, so continue with `next_cursor`; the second is `confirmed_absent` for that exact text only, and
  the near miss says to search again for the longer form, `0417382` (sections 02, 03 and 05 supply each step).
- **Shape and length:** field guide. Thirteen sections: ten ideas in dependency order, then worked cases,
  reference and evidence. About 8,300 words including tables and captions; no length was stated, so it is set by
  the nine tools and the ten ideas.
- **Assumptions made without asking** (the brief was settled): the sample-data cast is reused unchanged; British
  English; no byline; the earlier `docs/guide.html` is untouched.

## Claims and evidence

| Claim | Status | Source |
|---|---|---|
| Nine read-only tools; names, arguments, fields, outcomes, error types | measured | `src/csv_o_matic/server.py`, `service.py`; checked by script against the page's tables |
| Limits and defaults (25 rows, 60,000 bytes, 120 s, 4 MiB reads ...) | measured | `config.py` `Limits`; checked by script |
| Every response in an example viewer | measured | 67 real calls captured by `scripts/build_guide.py` from the sample data |
| Outcome rules, near misses, cursors, row and time limits, comparison statuses, behaviour on broken files | modelled, checked | the page's JavaScript miniature; identical to the real service on 3,218 cases (6,702 search calls), wording included |
| A cursor chain can stall on a record longer than one read under a budget too short for a second read | measured at small scale | real server, 48-byte reads, 1 ns budget (`stall_1..3`); also reproduced with `chunk_bytes == max_record_bytes` |
| ... and at default sizes that needs a record over 4 MiB | inferred | from `csvscan.iter_records`; not run at 4 MiB |
| Formatting-only differences in a comparison are listed as changed, with a note | measured | real service on a throwaway file (not in the sample data, not shown) |
| A cursor is refused for another search, a changed file, or a restarted server | measured | real service, three runs |
| Positions started after 21:15 would read the rerun (Larkspur 455.0000, Pendle joined, 6 rows) | modelled | rule quoted from `process_maps/eod_positions.yaml`; result checked against `make_sample_data.positions()` |
| Positions at 20:00 hold Larkspur at 452.1100 and no Pendle | measured | captured responses |
| 380-495 MB/s, 63 MB peak, stops in 3-15 ms | reported, not re-run | README |
| 20 MB/s network share | assumed | a round number to show a file that outlasts its budget |
| 6 GB file; every company, portfolio, identifier and price | illustrative | invented |

## Sequence of models

1. **Why can't the model just read the file?** Simplest model: hand over the file. It fails on size (6 GB against a
   60,000-byte response). Repair: one narrow question, the server reads, matching rows return. Teaches the
   encoding.
2. **What if the scan has not finished?** Model 1 assumed every scan reaches the end. Adds: three outcomes;
   `inconclusive` is "not yet", not "absent".
3. **How does a stopped scan finish?** Model 2 leaves "continue" undefined; calling again repeats the same reads.
   Adds: `next_cursor`, the first-read guarantee, and its edge.
4. **Does one match mean every match?** Model 3 treated time as the only reason to stop. Adds: the row and byte
   limits, `more_matches`, `count_matching_rows`.
5. **What counts as a match?** Models 1-4 took "match" for granted. Adds: exact whole-field text, near misses and
   their one-directional blind spot.
6. **What if the column is wrong?** Model 5 held the column fixed. Adds: a true answer to the wrong question,
   `search_across_columns`, and the two header tools.
7. **Which file?** Models 1-6 assumed the file was chosen. Adds: the process map, folders, file listings, business
   date against run timestamp, and why two deliveries disagree.
8. **Two files, one security.** Adds: `compare_security_records`, and why it refuses to pair records without a key.
9. **What if the file is broken?** `confirmed_absent` assumed the file was read as intended. Adds: wrong delimiter,
   unclosed quote, wrong encoding; the server answers `inconclusive`.
10. **Sandbox.** Text, column, row limit and time budget together, on the clean file.
11. Worked cases, 12. Reference, 13. Evidence and limits.

## Argument in plain text

A model cannot read a multi-gigabyte file, so the server reads it for the model and returns only the rows that
match one exact value in one column, with a statement of how far it read.

That statement matters more than the rows. A scan can stop before the end, on time or on size. If it has seen a
match it says `match_found`; if it has seen none it may only say `inconclusive`, because absence is a claim about
the whole file. `confirmed_absent` and `more_matches: none` both cost a complete scan. A stopped scan returns a
cursor; without it, a second call repeats the first.

`confirmed_absent` is exact about the question asked: this text, in this column, of this file. Change any of the
three and the answer may change. The server helps with the first two through near misses (it reports values that
contain the text, or the text in another column) but cannot see the reverse case, a shorter or differently cased
form. The third is the model's job, with the process map as its guide: file names carry the business date and run
timestamp, and a downstream step reads whatever run existed when it started.

Comparison follows the same ethic: when two files each hold several records for a security, the server will not
guess which pairs with which. And when a file cannot be read as intended, a clean miss is reported as
`inconclusive`, never as absence.

So the reader's checklist is: did the scan complete; if not, continue it; if it found nothing, was it the right
text, column and file. Only then is "absent" a finding.

## Figures

Every file figure uses the same object, the custodian file of 13 November (8 records, 4 columns), the same renderer
and the same marks: a margin bracket is one read, hatching is "not read", a filled cell is an exact match, a dashed
outline is a near miss, a ruled line is where a call stopped. The file is on the left (the author's view, which the
model never sees) and the response on the right (all the model sees). Each has a reset, a live line computed from
the model result, and a static summary for print and for readers without JavaScript.

| # | Reader question | Initial case | Action | Expected observation | Why | Held fixed | Boundary | Controls |
|---|---|---|---|---|---|---|---|---|
| 1 | What happens in what order? | Scan for `0417382` complete, 4 reads | Step through the reads | Both matches appear in read 1; the response is sent only after read 4 | The server cannot say "all" until it has seen every row | The call, the file | Before any read: nothing returned | Stepper only, no model inputs |
| 2 | What can be claimed when the scan stops here? | Pendle, clock stops after 2 of 4 reads: `inconclusive` | Drag to 4 reads; switch to Corbel | Present value flips to `match_found` when its row is read; absent value becomes `confirmed_absent` only at 4 | Finding takes one row, ruling out takes all | Column, file | Halvorsen after 1 read: `match_found` with `more_matches: unknown` | Target (3 choices), reads 1-4. Prediction prompt first |
| 3 | Why does the current approach fail? | 1 read per call, cursor passed: 4 calls | Switch the cursor off; then set the budget to nothing | Without it every call re-reads records 2-3; with no time the chain still takes 4 calls | A second call has the same budget and start; the first read is exempt from the clock | Target (Pendle), file | Budget 0; budget 4 (one call) | Reads per call 0-4, cursor toggle |
| 4 | Where does a full response stop? | PF001, `max_rows` 2: 2 calls | Move the limit to 4 | Call 1 stops at the third match, so it can say `yes`; at 4 it reads on to the end before saying `none` | A size stop has seen the next match; `none` needs the end of the file | Value, column | `max_rows` equal to the number of matches | `max_rows` 1-5 |
| 5 | Which rows equal this text? | `0417382` in SecurityId: records 2, 3 | Search for `417382`, then `b7k2mn4` | The short form finds record 5 and reports the long form as near misses; the long and the lower-case forms report nothing | Near misses report values that contain the text | Column | `b7k2mn4`: clean `confirmed_absent` beside `B7K2MN4` | Value (4 choices). Prediction prompt first |
| 6 | Which column was asked? | PF003 in SecurityId: `confirmed_absent` | Change the column | Absent in three columns with "exact value in other column", found in PortfolioCode and in any column | A match is a value in a column | Value | Any column: a different tool, naming the column | Column (5 choices) |
| 7 | Which version behaves differently? | Positions start 20:00, reads the 18:30 golden file | Move the start past 21:15 | Larkspur's price and Pendle's presence change together when the start crosses the rerun | The step reads the latest run that existed when it started | Vendor and golden events | Just before 21:15: unchanged | Start time 18:40-22:00, step 10 min |
| 8 | Which record goes with which? | Halvorsen, no key: `ambiguous_alignment` | Choose PortfolioCode | Lines appear only when the key picks one record per side | Nothing in a CSV promises order | Files, compared columns | SecurityId as key: ambiguous again; Tidewater: partial; Pendle: one side only | Security (4), key (3) |
| 9 | What does the parser see? | As delivered: Pendle is record 9 | Break the file three ways | One column; one swallowing field; no text at all. Always `inconclusive` | A miss in a misread file proves nothing | Search (any column, `0058213`) | A match in the broken file is flagged partial (real example) | Fault (4 choices) |
| 10 | Does the rule survive other cases? | One clean call | Prediction, then three named cases | Outcome follows from text, column and how far the scan gets | Sections 02-06 combined | The clean file | Match on call 2, completeness on call 4 | Value, column, `max_rows`, reads |

Static equivalent of each: the `.fig-summary` beside it, which states every teaching case in words.

**Rejected or cut**

- *A pipeline-presence matrix* (each security against each folder): a static table teaches it as well, and worked
  case 3 shows the one interesting drop with real responses.
- *A slider for the output-size limit*: only a number changes, and the miniature file never approaches 60,000
  bytes. Covered in a sentence and the limits table.
- *Faults in the sandbox*: with tiny reads a damaged file can stall a chain (the edge reported in section 03), which
  would teach a rare fault as normal behaviour. Faults keep their own figure.
- *Progress, queueing and cancellation as a figure*: real elapsed time cannot be shown honestly on a 234-byte file.
  Prose, with the source of each number in the margin.
- *The earlier guide's JSON-first panels and multi-slider workbenches*: replaced by one-line evidence viewers that
  open on demand, and by one control or two per figure.

## Model

One block of JavaScript in the page (`CSVM`, between the `MODEL` markers), pure, no DOM.

- **`searchCall` / `investigate`**: inputs are the file (custodian; fault none, semicolon, quote or utf16), the
  search text, the column or "any", count mode, `max_rows`, the time budget counted in reads (0 to 4, or none), the
  read size (48 bytes for scan figures so the file takes four reads; 8,192 for the fault figure) and a cursor.
  Rules re-implemented from `csvscan.py` and `service.py`: reading in chunks cut at the last line end with carry;
  record splitting by the `csv` module's rules; the first read exempt from the clock; exact whole-field matching;
  the four kinds of near miss; the row limit stopping at the next match; cursor state (offset, record, matches so
  far); layout and unterminated-quote warnings; the outcome, `more_matches` and count rules; the explanation text.
- **`compareRecords`**: grouping by match keys and the six statuses, for complete scans.
- **`eveningModel`**: start time in minutes; picks the latest golden run before it.
- **Invariants checked:** `confirmed_absent` only when the scan is complete and there are no warnings;
  `more_matches: none` only at end of file; a chain followed with its cursors returns every match exactly once and ends with the outcome one unlimited call would give (1,250 chains).
- **Not modelled:** the output-size limit, non-ASCII encodings, bare-CR line ends, files changing mid-scan, real
  time, concurrency, cancellation.
- **Concept-level control:** "time budget in reads" stands in for `max_scan_seconds`. On the real service the same
  cases were run with a clock that expires after a set number of reads, so both sides stop in the same place; the
  real seconds-based limit is shown by the captured chain (`chain_1..4`, a 1 ns budget).

**Teaching cases, run through the model**

- Scan for `0417382`: reads [2,3] [4,5] [6,7] [8,9]; `match_found`, records 2 and 3, `more_matches: none`.
- Pendle by reads 1-4: inconclusive, inconclusive, inconclusive, match_found (record 9). Corbel: inconclusive x3,
  confirmed_absent. Halvorsen: match_found throughout, `more_matches` unknown x3 then none.
- Cursor, 1 read per call: 4 calls, stops at bytes 96, 140, 186, then end. Budget 0: the same 4 calls. No cursor:
  every call reads [2,3].
- PF001 with `max_rows` 2: call 1 returns 2 and 4 and stops at record 6 (byte 140), `yes`; call 2 returns 6 and 9,
  `none`. `max_rows` 4 or 5: one call. `max_rows` 1: four calls.
- `417382`: record 5, two near misses (2, 3). `b7k2mn4`: confirmed_absent, no near misses.
- PF003: absent in SecurityId, Quantity, SettlementCcy (near miss: PortfolioCode); found in PortfolioCode and any.
- Evening: before 21:15, price 452.1100, 5 rows; after, 455.0000, value 18200.00, Pendle 5827.50, 6 rows.
- Pairs: Halvorsen none/PortfolioCode/SecurityId = ambiguous/compared/ambiguous; Tidewater by PortfolioCode =
  partially_compared (record 6 alone); Pendle = present_in_one_file_only; Larkspur = compared.
- Faults for `0058213` in any column: match_found; inconclusive (delimiter warning); inconclusive (unterminated
  quote, 3 records after the header); inconclusive (`unsupported_encoding`).
- Sandbox prediction (PF003, any column, 1 row, 1 read): inconclusive, match_found (record 5), match_found,
  match_found with `more_matches: none` on call 4.

## Review

- `python scripts/build_guide.py --guide docs/field-guide.html` embeds the 67 real responses (the page asks for the
  extra set with `<!--EXAMPLES:SET field-guide-->`).
- `python scripts/build_guide.py --guide docs/field-guide.html --check-model` (needs node): miniature against real
  service, 3,218 cases, 6,702 search calls, 0 differences.
- A script ran the page's model on every teaching case and boundary above and compared the results, and the
  embedded real responses, with every number and quoted phrase in the hero, prose, summaries and tables: 62
  checks, 0 failed. A second script checked the limits table, tool names, error types and quoted product wording
  against the code.
- The skill's `check_page.js`: no problems and no warnings at 320, 1440 and 2560 px, in Light and Dark, with
  JavaScript off and in print. Screenshots of every figure at 1440 and 320 px were inspected.
- Understanding: the hero, headings, first sentences and bold lines carry the argument; the unfamiliar case is
  answerable from sections 02, 03 and 05.

## Deviations from the skill, and why

- The `.fig-summary` under each figure is hidden while JavaScript is running and shown in print and without it.
  With a live line and a paragraph after each figure, a third statement of the result read as repetition.
- The sandbox is a wide workbench in the reading flow, not a sticky side panel: with a file and a chain of
  responses on stage it did not fit a sticky column at laptop heights.
- Example viewers show one line (tool, outcome, the server's own explanation) and open on demand, so the evidence
  is present without the page reading as a wall of JSON.
- Two small fixes to the template's framework in this page: a button-group control no longer prints a numeric
  readout (it showed "NaN" for text values), and the scrubber gets its own row as the template's CSS intended.
- Tag wording: "Measured" covers both constants read from the code and responses captured from the server; the
  page says so in its evidence note.
