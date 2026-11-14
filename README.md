# CSV-o-Matic

A read-only MCP server that lets an LLM investigate individual securities across large CSV
files belonging to different ETL processes.

A user describes a problem ("security X is missing from the client report", "this price looks
wrong"). The LLM asks the server how the process works, decides which folders and files to
look at, searches them for the security, and explains what it finds. The server's job is to
return accurate, bounded evidence about the files it is asked about. Choosing files and
interpreting results is left to the LLM.

- **Read-only.** Files are only ever opened for reading. Only folders named in a process map
  can be reached.
- **No database, no index, no cache.** Process maps are re-read on every request; folder
  listings and CSV data are read from disk on demand.
- **Built for multi-gigabyte files.** Files are streamed in fixed-size chunks. Memory use does
  not grow with file size, and no file is ever returned whole.

## Quick start

Requires Python 3.11+.

```bash
cd csv-o-matic
pip install -e ".[dev]"

python scripts/make_sample_data.py          # sample data matching the example process maps
csv-o-matic --config config.example.yaml    # stdio transport (default)
```

Register it with an MCP client, for example:

```json
{
  "mcpServers": {
    "csv-o-matic": {
      "command": "csv-o-matic",
      "args": ["--config", "/path/to/config.yaml"]
    }
  }
}
```

`--transport streamable-http` serves over HTTP instead of stdio. The config path can also be
given in the `CSV_O_MATIC_CONFIG` environment variable.

## Configuration

See [`config.example.yaml`](config.example.yaml). Relative paths resolve against the config
file's directory.

| Key | Meaning |
| --- | --- |
| `data_root` | Base directory for relative folder paths in process maps |
| `process_maps_dir` | Directory of process map files (`.yaml`, `.yml`, `.json`) |
| `limits.max_rows_per_response` | Matching rows returned by one call (default 25) |
| `limits.max_output_bytes` | Hard ceiling on the size of any tool response (default 60 000) |
| `limits.max_cell_chars` | Longer cell values are cut and flagged (default 2 000) |
| `limits.max_sample_rows` | Ceiling for header-inspection sample rows (default 10) |
| `limits.max_files_per_listing` | Files returned by one listing call (default 200) |
| `limits.max_concurrent_scans` | File scans running at once; others queue (default 2) |
| `limits.max_scan_seconds` | Time budget per scan (default 120) |
| `limits.progress_interval_seconds` | Interval between progress notifications (default 2) |
| `limits.chunk_bytes` | Read size; memory per running scan is a small multiple of this (default 4 MiB) |
| `limits.max_record_bytes` | Longest single CSV record accepted (default 16 MiB) |

## Process maps

One file per process, maintained by hand in `process_maps_dir`. Two worked examples are in
[`process_maps/`](process_maps/); `security_master.yaml` shows a branching process. Unknown
keys are rejected so that typos are noticed: a map that fails validation is left out and
reported under `process_map_load_errors` in the tool responses.

```yaml
id: security_master                  # unique id
name: Security Master Build
description: One line, shown when listing processes.
purpose: What the process is for.
schedule: Optional.
run_semantics: >                     # how runs, retries, reruns and backfills show up in the files
  Several files with the same business date are retries; the latest run timestamp wins ...

folders:
  - id: vendor_raw
    path: security_master/01_vendor_raw      # relative to data_root, or absolute
    description: What the folder holds and its grain.
    filename_convention:
      pattern: "vendor_ref_{business_date}_{run_timestamp}.csv"     # for humans
      regex: '^vendor_ref_(?P<business_date>\d{8})_(?P<run_timestamp>\d{8}T\d{6})\.csv$'
      tokens:                                 # one entry per named group; 'format' marks dates/times
        business_date: {format: "%Y%m%d", meaning: "As-of date of the data"}
        run_timestamp: {format: "%Y%m%dT%H%M%S", timezone: UTC, meaning: "When the run started"}
      default_date_token: business_date       # optional; what date filters use by default
      notes: Optional.
    identifier_columns:
      - {column: SEDOL, identifier_type: SEDOL, notes: "7 characters, may start with zeroes"}
    record_key_columns: [ISIN, ExchangeCode]  # what identifies one record for a security
    csv_format: {delimiter: ",", encoding: utf-8}   # optional; these are the defaults

steps:
  - id: normalise
    name: Normalise
    description: What the step does.
    reads: [vendor_raw]                       # folder ids
    writes: [normalised]
    downstream: [enrich_equity, enrich_fixed_income]   # several = a branch
    upstream: []                              # optional; either end of a link may declare it
    branch_condition: Which records take this branch.
    field_mappings:
      - {from_folder: vendor_raw, from_column: ISIN, to_folder: normalised, to_column: isin, rule: "Trimmed, upper-cased"}
    transformation_rules: ["Reads only the latest run_timestamp for the business date."]
    filter_rules: ["Rows with an empty ISIN are dropped."]
```

A folder used by several processes (one process's output is another's input) can be declared
in each map; the folder inventory merges them by physical path.

Supported encodings: `utf-8` (a byte-order mark is ignored), `ascii`, `latin-1`, `cp1252`.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_process_map` | List processes, or return one process's full map (or one step with `step_id`) |
| `list_folders` | Folder inventory from the maps: path, description, steps that read/write it |
| `list_csv_files` | Actual files in a folder: name, size, modification time, parsed filename dates; filename and date filters |
| `inspect_csv_headers` | Column names in original order; optional small sample of leading rows |
| `search_column_names` | Find columns by name fragment without fetching the whole header |
| `find_security_in_file` | Rows where a chosen column equals an identifier exactly; whole row or chosen columns |
| `search_across_columns` | Rows where any column equals the identifier; names every matching column |
| `count_matching_rows` | Exact count of matching rows, without row contents |
| `compare_security_records` | Find a security in two files and compare fields, with column mappings and match keys |

### How to read results

Every search result carries the file, the search parameters, and a `scan` block saying how far
the scan got and why it stopped.

- `outcome` is one of:
  - `match_found` - at least one exact match was seen.
  - `confirmed_absent` - the whole file was scanned and nothing matched.
  - `inconclusive` - neither can be claimed: the scan was interrupted, the file could not be
    parsed or read, a column was missing, or the file changed while being scanned. An
    `explanation` (and `error` where relevant) says which.
- `results_are_partial` / `more_matches` (`none`, `yes`, `unknown`) say whether every match was
  returned. A match never implies that all matches were returned.
- `next_cursor` continues a search that stopped at a row limit, the output-size limit or the
  time limit. Cursors are stateless tokens: they hold a file position, are tied to the search
  and to the file's size and modification time, and stop working if the file changes or the
  server restarts.
- `count_matching_rows` returns `count` only after a complete scan. Otherwise `count` is
  `null`, `count_is_exact` is `false`, and `matches_seen_before_interruption` is a lower bound.
- `compare_security_records` reports `comparison_status`: `compared`, `partially_compared`,
  `ambiguous_alignment`, `present_in_one_file_only`, `absent_from_both` or `inconclusive`.
  When a file holds several records for the security and no `match_keys` are given, nothing is
  paired or compared; the response lists the records and the columns that tell them apart.

Matching is exact, case-sensitive, whole-field text matching. Values are never converted, so
`00123` and `123` are different identifiers and leading zeroes are kept. Values that equal the
identifier only after trimming whitespace are not matches, but are counted in a warning.
In comparisons, values that differ only in whitespace, case or number formatting are still
listed as changed, with a note.

`record_number` is 1-based and counts the header as record 1, so it equals the spreadsheet row
number when no field contains a line break.

Column names are matched exactly. Where a header repeats a name, rows label the columns
`name [col N]` and a column can be selected as `#N`.

`filesystem_modified_time` is when a file was last written on disk. Business dates and run
timestamps come only from the filename, parsed with the folder's convention, and are returned
separately as `filename_tokens`.

### Limits, progress and cancellation

- Every response is kept under `max_output_bytes`. Rows that do not fit are left for the next
  cursor; a single row too wide to fit is returned with trailing columns omitted and flagged.
- Scans run in worker threads, at most `max_concurrent_scans` at once. Others wait their turn;
  waiting does not use up the time budget.
- A scan that reaches `max_scan_seconds` (or the caller's lower `max_scan_seconds` argument)
  stops and returns what it found, marked incomplete, with a cursor to continue.
- During a scan the server sends MCP progress notifications (bytes scanned of total, matches
  so far) to clients that supply a progress token.
- Cancelling the request (`notifications/cancelled`) stops the scan within one chunk -
  milliseconds in practice - and frees its slot.

**Cancellation and partial results.** The MCP protocol does not deliver a response to a request
the client has cancelled, so a client-cancelled call cannot itself report "incomplete, partial
results". The scanner does produce that result (`stop_reason: cancelled`, matches marked
partial, count withheld) and it is covered by tests, but over MCP the way to *receive* an
interrupted result is the time limit: pass `max_scan_seconds`, and the response comes back
marked incomplete with the partial matches and a cursor.

## CSV handling

Files are parsed by the rules of Python's `csv` module: delimiter-separated, double-quote
quoting, doubled quotes as escapes, LF or CRLF line ends. Quoted fields may contain delimiters
and line breaks. Other dialects (backslash escapes, a different quote character, CR-only line
ends) are not supported.

To be fast on large files the scanner skips, without parsing, lines that cannot contain the
identifier and are provably complete records; everything else goes through the `csv` module.
The tests check that this gives exactly the `csv` module's answer on hand-written edge cases
and several hundred randomly generated files, including files with broken quoting.

Problems are reported, not guessed around: a record longer than `max_record_bytes` (usually an
unterminated quote) stops the scan as a parse error; a file ending inside a quoted field, or
modified during the scan, downgrades "absent" to "inconclusive". Rows with more or fewer fields
than the header are returned with a `field_count_mismatch` note.

Folders are flat: `list_csv_files` lists `*.csv` files directly inside a mapped folder.
Compressed files are not read.

## Verification

```bash
python -m pytest                                              # unit, tool and MCP protocol tests
python scripts/acceptance_large_file.py --gb 2                # large-file acceptance
python scripts/acceptance_large_file.py --gb 2 --style quote_all
```

The test suite covers quoted fields, embedded commas and newlines, leading-zero identifiers,
duplicate matches, wide (600-column) headers, row and output-size truncation with cursor
paging, unsuccessful searches, missing columns, parse failures, time limits, cancellation,
ambiguous comparisons, the concurrency cap, and that source files are byte-for-byte unchanged.

The acceptance script generates a 120-column file of the requested size and checks results,
peak memory, time-limit behaviour, cancellation latency and that the file is untouched.
Measured on a 4-core cloud VM with the file in the page cache (2 GB files):

| File style | Scan speed | Peak memory | Stop after cancel |
| --- | --- | --- | --- |
| Mixed (some quoted fields, occasional embedded newlines) | ~500-590 MB/s | 63 MB | 10 ms |
| Every field quoted | ~170 MB/s | 75 MB | 6 ms |

Real speeds depend on storage: on a network share, I/O will usually be the limit.

## Layout

```
src/csv_o_matic/
  config.py        configuration and limits
  process_maps.py  process map schema, validation, folder inventory
  csvscan.py       streaming CSV reader
  service.py       the nine tools as plain functions
  server.py        MCP wiring: tool definitions, progress, cancellation, concurrency
process_maps/      example process maps
scripts/           sample data generator, large-file acceptance check
tests/
```
