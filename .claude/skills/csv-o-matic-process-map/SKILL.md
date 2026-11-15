---
name: csv-o-matic-process-map
description: Write, extend, fix or review the process map files (YAML/JSON in process_maps/) that the CSV-o-Matic MCP server reads to learn how an ETL process works - its folders, filename conventions, identifier columns, steps, field mappings and filter rules. Use this whenever someone wants to add a new process or folder to csv-o-matic, document a pipeline so the server can investigate it, turn ETL code, a data folder or a verbal description into a process map, or when a map is not loading, shows up under process_map_load_errors, a folder or process is missing from list_folders / get_process_map, files show matches_filename_convention false, date filters return nothing, or searches fail with process_map_conflict or wrong-delimiter warnings. Use it even if the user only says "map", "onboard this feed" or "teach csv-o-matic about X".
---

# Writing process maps for CSV-o-Matic

A process map is one hand-maintained file per ETL process. The CSV-o-Matic server has no other
knowledge of the world: only folders named in a map can be read, and everything the
investigating model knows about a process - where to look, which file is the current one, which
column holds the identifier, why a security might legitimately disappear - comes from the map.

So a map is read as testimony. When someone asks "why is this security missing from the client
report?", the model will answer with what the map says. That sets the standard:

- **A wrong statement is worse than a missing one.** An invented filter rule becomes a confident,
  false explanation given to a user. Write what the code, the data or the process owner
  establishes. Where you could not establish something, say so in `notes` rather than fill the
  gap with something plausible.
- **Names must be exact.** The server matches column names and filenames exactly and
  case-sensitively. `ISIN` and `isin` are different columns. Copy names from real headers.
- **The map has to agree with the files on disk**, not with how the process was designed.

## Workflow

The two scripts mentioned below are in this skill's `scripts/` folder. The commands show the
path when the skill is installed in the repo; if it lives elsewhere, use that folder's path.

### 1. Find your bearings

- The repo's `README.md` ("Process maps") and `process_maps/security_master.yaml` are the house
  style: read the example before writing. `src/csv_o_matic/process_maps.py` is the schema.
- The server's config file (`config.yaml`, or `config.example.yaml`) gives `process_maps_dir`
  (where the map goes) and `data_root` (what relative folder paths resolve against).
- Read the maps already in `process_maps_dir`. A folder another process already declares must
  be described compatibly (see "Shared folders").

### 2. Gather evidence

Use whatever exists, in roughly this order of reliability:

1. **The ETL code or job configuration.** The only reliable source for filter rules, joins,
   renames and which input file a step picks. Read it for every way a row can fail to come out:
   inner joins, `dropna`, `WHERE` clauses, de-duplication, status filters, type-conversion
   failures that are swallowed.
2. **The data itself.** Run the survey script on the data folders:

   ```bash
   python .claude/skills/csv-o-matic-process-map/scripts/survey_data.py <data_root>/<process folder>
   ```

   It prints, per folder, the filename shapes in use, which date formats the digits are
   consistent with, the encoding and delimiter, the exact header and a few rows. It reads only
   the start of a few files, so it is safe on multi-gigabyte data. Data tells you names, formats
   and grain. It does not tell you rules.
3. **Existing documentation and the user.** Some things are only in someone's head: what a
   second file for the same date means, which one downstream consumes, how backfills are named,
   the schedule. Ask for these specifically, in one batch, once you know what the code and data
   leave open. If nobody is available to ask, carry on and record each open point (next section).

### 3. Draft the map

One file per process, named `<id>.yaml`. `references/schema.md` describes every field and how
each tool uses it; read it when you write a kind of field for the first time. The parts that
most affect whether investigations come out right:

**`run_semantics`** - the most valuable paragraph in the file. State how runs, retries, reruns,
redeliveries and backfills show up as files; which file wins when there are several for a date;
whether files are overwritten or accumulate; which upstream file a step picks (latest at the
time it ran?); and whether filesystem modification time can be trusted. The classic
investigation ("it was added by a later rerun, and this file was built before that") is only
possible if this is written down.

**`filename_convention`** - the regex is applied with full match to the bare filename. Give
every variable part a named group and a `tokens` entry: a group without a token is silently
discarded. Date and time tokens need a `format` (strptime), or they cannot be filtered on. If a
folder has more than one date token, set `default_date_token` to the business date. Optional
parts (`(?P<backfill>_BF)?`) keep one convention covering variants. A file that matches the
regex but whose date does not parse counts as not matching.

**`identifier_columns` and `record_key_columns`** - exact header names. Add `notes` for anything
that defeats exact matching: leading zeroes, padding, a prefix, mixed identifier types in one
column. `record_key_columns` is what makes one record unique for a security (a security often
has several rows: one per listing, per portfolio, per date) and is what
`compare_security_records` needs to pair rows.

**`csv_format`** - state it only when it differs from comma + utf-8, or when you have verified
it and want the map to assert it. Stating it gives this map a vote on a shared folder.

**`steps`** - one per stage that reads and/or writes folders. For each, ask "how could a
security go in and not come out?" and put every answer in `filter_rules`, in plain words with
the actual column names and values (`Rows where Status is 'DELISTED' are dropped`). Include
losses that are side effects: an inner join, a branch nobody takes ("asset_class OTHER enters
neither branch"). If a step drops nothing, say that in `notes`, so absence is not blamed on it.
Use `transformation_rules` for how values change and which input file is chosen;
`field_mappings` for renames and derived columns (put the rule in `rule`); `branch_condition`
on each step after a fork.

**Unknowns** - when a fact could not be established, write it where the reader will see it:
the step's or folder's `notes`, or the top-level `notes`, e.g.
`Not confirmed: whether rows with a zero price are dropped at this step.` Keep these apart
from stated rules; never put a guess in `filter_rules`.

YAML traps: single-quote every regex (`'^x_(?P<d>\d{8})\.csv$'`) so backslashes survive;
quote strptime formats and anything that looks like a time, date, number or boolean
(`"18:00"`, `"%Y%m%d"`, `"Y"`), because the schema wants strings and rejects the rest; a tab
delimiter is `"\t"` in double quotes. Unknown keys are rejected, so do not add fields of your
own - extra information goes in `notes`.

### 4. Check it

```bash
python .claude/skills/csv-o-matic-process-map/scripts/check_process_map.py <map file> --config <config.yaml>
# or: --maps-dir <process_maps_dir> --data-root <data root>
```

Run it from inside the csv-o-matic repo (or pass `--repo`). It applies the server's own schema,
then lints, then - when it knows the data root - compares the map with the real files: whether
folders exist, whether every filename matches the regex and its dates parse, whether each
column the map names is in the real header, whether delimiter and encoding hold. A draft kept
outside `process_maps_dir` can be checked alongside the live maps by also passing `--maps-dir`.

- `ERROR`: the server would reject the map or refuse the folder's files. Fix all of them.
- `WARN`: the map loads but a tool will mislead or go quiet. Fix, or decide deliberately and
  explain in `notes` (for example stray files that do not belong to the process).
- `INFO`: worth a look; often fine.

Without data access the first two layers still run. Say in your report that the map was not
checked against real files.

A map that fails validation is not an error anyone sees at startup: the server leaves it out
and mentions it under `process_map_load_errors` in tool responses. Maps are re-read on every
request, so once the file is in `process_maps_dir` no restart is needed.

### 5. Hand over

Tell the user, briefly:

- where the file is and that the checker passes (or what remains and why);
- what was verified against real data, and what rests only on code, documents or their word;
- the open questions you recorded as "not confirmed", so the owner can close them.

## Fixing or extending an existing map

Start with the checker; its messages name the folder or step and say what the server does
about it. Typical causes: a misspelt or invented key (the whole map is dropped); a token that
is not a named group in the regex; a step referring to a folder id that does not exist; a regex
that no longer matches real filenames after a naming change; a column name with the wrong case.
Fix the cause in the file - do not work around it by deleting the detail. When filenames or
headers changed over time, keep the old files reachable: widen the regex with an optional
part, and record the change of layout in `notes`.

When adding a folder or step, re-check the links: `upstream`/`downstream` may be declared from
either end, and every step that forks needs a `branch_condition` on its branches.

## Shared folders

When one process's output is another's input, each map declares the folder under the same
`path` (ids may differ). The server merges them by physical path:

- If two maps state different `csv_format`s, every file in the folder is refused
  (`process_map_conflict`). In the consuming map, leave `csv_format` out unless it is identical.
- The filename convention is taken from the first map (in file-name order) that has one. In
  the consuming map either omit it or copy it exactly.
- Identifier columns are pooled; descriptions are shown per process.

Describe the folder from the consuming process's point of view (`Golden security master
published by the Security Master Build; read only by this process`) and say in `run_semantics`
which of its files the consumer picks.

## Limits to know

- Folders are flat: only regular `.csv` files directly inside a declared folder are visible.
  Subfolders (per-date directories, archives) each need their own folder entry, or stay out of
  reach - say so in `notes` if relevant.
- Every file is assumed to have a header row. Headerless files cannot be described.
- Encodings: `utf-8` (a BOM is fine), `ascii`, `latin-1`, `cp1252`. UTF-16 exports and
  compressed files are refused. The delimiter is one character; quoting is standard
  double-quote only.
- A map describes; it cannot express logic. Anything conditional goes into prose fields.
