# Process map schema

Source of truth: `src/csv_o_matic/process_maps.py` in the repo. If this file and the code
disagree, the code wins - and `scripts/check_process_map.py` uses the code. Unknown keys are
rejected at every level. All values are strings unless shown as a list or mapping; YAML values
that parse as numbers, booleans, dates or times are rejected, so quote them.

Required fields are marked **R**.

## Top level

| Field | | What to write | Used by |
| --- | --- | --- | --- |
| `id` | R | Short unique id, e.g. `security_master`. Unique across all map files, ignoring case. | `get_process_map`, `list_folders` scope |
| `name` | R | Human name. Also accepted when looking a process up. | process list |
| `description` | R | One line. This is all a model sees when listing processes, so say what goes in and what comes out. | process list |
| `purpose` | R | A few sentences: what the process is for, who consumes its output. | full map |
| `schedule` | | When it runs, with timezone. Quote it. | full map |
| `run_semantics` | R | How runs, retries, reruns, redeliveries and backfills appear as files; which file wins; whether mtime is trustworthy. | full map; decides which file an investigator opens |
| `folders` | R | List, below. | |
| `steps` | R | List, below. | |
| `notes` | | Anything else, including what is not confirmed. | full map |

## `folders[]`

| Field | | What to write | Used by |
| --- | --- | --- | --- |
| `id` | R | Unique within the map. Steps refer to it. | step `reads`/`writes`, mappings |
| `path` | R | Relative to `data_root` (preferred), or absolute. This string is what callers pass as `folder`. | every file tool |
| `description` | R | What the folder holds **and its grain** ("One row per listing"). | `list_folders` |
| `filename_convention` | | Mapping, below. Omit only when another map declares it for the same path, or filenames carry no information. | `list_csv_files` |
| `identifier_columns` | | List of `{column, identifier_type, notes}`. `column` is the exact header name. `identifier_type` is free text (`ISIN`, `SEDOL`, `CUSIP`, `vendor internal id`). | `list_folders`; tells the model which column to search |
| `record_key_columns` | | List of exact header names that together identify one record. | guides `match_keys` in `compare_security_records` |
| `csv_format` | | `{delimiter: ",", encoding: utf-8}` are the defaults. Delimiter: one printable ASCII character or tab. Encoding: `utf-8`, `ascii`, `latin-1`, `cp1252`. | every scan; conflicts across maps block the folder |
| `notes` | | Layout changes over time, stray files, quirks. | full map |

### `filename_convention`

| Field | | What to write |
| --- | --- | --- |
| `pattern` | R | For humans: `prices_{business_date}_{run_timestamp}.csv`. Use `{token}` for each named group; `[...]` for optional parts. |
| `regex` | R | Python regex, full-matched against the bare filename. One named group `(?P<name>...)` per variable part. Single-quote it in YAML. |
| `tokens` | | One entry per named group: `{meaning: "...", format: "%Y%m%d", timezone: UTC}`. `meaning` is required. `format` (strptime) marks the token as a date or time and makes it filterable; without `format` the raw text is returned. Every token must be a group in the regex. |
| `default_date_token` | | Which dated token date filters use when the caller does not say. Defaults to the first token with a `format`. Must have a `format`. |
| `notes` | | E.g. "A second file for the same business_date is a vendor redelivery." |

How the server uses it: a file that full-matches and whose dated tokens all parse gets
`matches_filename_convention: true` and `filename_tokens` in ISO form. Anything else is listed
with `matches_filename_convention: false` and is skipped by filename-date filters. Named groups
with no `tokens` entry are dropped without comment.

Common formats: `%Y%m%d` (20261113), `%Y-%m-%d`, `%Y%m%dT%H%M%S`, `%Y%m%d%H%M%S`, `%d%m%Y`,
`%H%M%S`. `strptime` is lenient about digit counts, so let the regex pin them (`\d{8}`).

## `steps[]`

| Field | | What to write | Used by |
| --- | --- | --- | --- |
| `id` | R | Unique within the map. | `get_process_map(step_id=...)`, links |
| `name` | R | Short human name. | |
| `description` | R | One sentence on what the step does. | |
| `reads` | | Folder ids. | `list_folders` role |
| `writes` | | Folder ids. | `list_folders` role |
| `downstream` | | Step ids fed by this one. Several = a fork. | process shape |
| `upstream` | | Step ids feeding this one. A link may be declared at either end; the server completes the other. | process shape |
| `branch_condition` | | On a step after a fork: which records take this branch. | explains "not in this branch" |
| `field_mappings` | | List of `{from_folder, from_column, to_folder, to_column, rule}`. Folder ids must exist; `from_folder` should be in `reads`, `to_folder` in `writes`. Column names exact. `rule` optional: how the value is carried or changed, or "Join key to golden.sedol". | supplies `column_mapping` for comparisons; explains value changes |
| `transformation_rules` | | List of sentences: calculations, rounding, which input file is picked. | explains differences |
| `filter_rules` | | List of sentences: every known way a record is dropped here. | explains absences |
| `notes` | | Unknowns, caveats, "this step drops nothing". | |

A step with only `writes` is an arrival (a file lands from outside). A step with only `reads`
is a delivery to something the server cannot see.

## Skeleton

```yaml
id: pricing
name: Daily Pricing
description: Turns the vendor close-price feed into the validated price file used by valuation.
purpose: >
  ...
schedule: "Weekdays, 17:30 UTC"
run_semantics: >
  ...

folders:
  - id: vendor_prices
    path: pricing/01_vendor
    description: Vendor price feed as delivered. One row per security and price type.
    filename_convention:
      pattern: "px_{business_date}_{run_timestamp}.csv"
      regex: '^px_(?P<business_date>\d{8})_(?P<run_timestamp>\d{8}T\d{6})\.csv$'
      tokens:
        business_date: {format: "%Y%m%d", meaning: "As-of date of the prices"}
        run_timestamp: {format: "%Y%m%dT%H%M%S", timezone: UTC, meaning: "When the delivery was received"}
      default_date_token: business_date
    identifier_columns:
      - {column: SEDOL, identifier_type: SEDOL, notes: "7 characters, may start with zeroes"}
    record_key_columns: [SEDOL, PriceType]
    csv_format: {delimiter: ";", encoding: cp1252}

steps:
  - id: receive
    name: Receive vendor prices
    description: Lands the vendor delivery unchanged.
    writes: [vendor_prices]
    downstream: [validate]

  - id: validate
    name: Validate prices
    description: ...
    reads: [vendor_prices]
    writes: [validated]
    field_mappings:
      - {from_folder: vendor_prices, from_column: SEDOL, to_folder: validated, to_column: sedol, rule: "Kept as text"}
    transformation_rules:
      - "Reads only the latest run_timestamp of vendor_prices for the business date."
    filter_rules:
      - "Rows where PriceType is not 'CLOSE' are dropped."
    notes: "Not confirmed: whether stale prices (unchanged for 5 days) are dropped here or only flagged."
```
