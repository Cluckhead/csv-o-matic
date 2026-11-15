# Changelog

## 0.2.0

### Folder access
- Mapped folders are approved roots; their genuine subfolders can be read too, and nothing else.
  `list_folders(folder=...)` lists one level of subfolders, paged by name (`start_after`).
- Folders are opened from the outermost root one component at a time without following links,
  and files relative to the directory reached. A mapped folder declared inside another one is
  reached the same way, so it cannot be redirected through a link.
- Where directory-relative opens are unavailable (Windows), subfolders are refused unless
  `allow_unconfined_subfolders` is set.
- Names are used exactly as on disk (no trimming; backslash is a separator only on Windows).
  Listings count links without examining their targets (`links_not_followed`).
- Shared folders: every filename convention is applied to every file (`filename_status`), never
  first-map-wins. Ambiguous or missing dates are reported separately from out-of-range ones.

### What a result can conclude
- Cursors carry what earlier pages saw (encoding problems, undecodable bytes, near misses), so a
  clean last page cannot certify absence or an exact count.
- Cursors are bound to the physical file (device, inode) and its change time; a same-size,
  same-timestamp replacement or in-place rewrite invalidates them. A metadata-only change during
  a scan is reported but no longer downgrades the result.
- Every continuation ends at a later record, even for records longer than one read.
- Header reading and sampling run under the time limit, progress and cancellation; samples take a
  scan slot, header-only reads do not. Partial samples are labelled.

### Columns, comparisons and filters
- One column selector everywhere: a name, `{"position": N}` or `{"name": "..."}`; ambiguous plain
  strings are refused. Returned labels never collide.
- Comparisons report how many columns were compared, warn when that is none or only identifiers
  (`no_comparable_columns` status), and list structurally missing fields and keys as missing.
  Invalid mappings are refused; unused ones are reported (`search.unused_column_mapping`).
- `filters` / `filters_a` / `filters_b`: exact record-key conditions applied during the scan and
  bound into cursors. A filtered miss is `no_match_within_filters`.

### Outputs
- All nine tools publish output schemas, with an `if`/`then` rule for the fields every
  successful response contains. Structured content and JSON text come from one validated
  payload; unknown fields are dropped rather than withholding the answer. `text_content: summary`
  shortens the text block.
- `max_output_bytes` covers the whole result (both copies): default 120 000, minimum 16 000.

### Compatibility
- Configurations with `max_output_bytes` below 16 000 are refused; one pinned at 60 000 returns
  about half as many rows per response as before.
- `linked_folders_not_followed` is now `links_not_followed`.
- In comparisons, one-sided missing fields moved from `changed` to `missing`.
