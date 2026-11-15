"""Streaming CSV record reader for very large files.

The reader walks a file in fixed-size chunks and never holds more than one chunk plus
one record in memory. It is built for *needle* searches: looking for one exact value in
a multi-gigabyte file. Most of a file can be skipped without parsing it:

1. Runs of lines that contain neither the needle nor a quote character cannot hold a
   match or a multi-line record, so they are only counted (plain byte searches).
2. A line with quotes but without the needle is skipped if its quoting proves it is one
   complete record (see `_is_single_line_record`).
3. Every other record - one that contains the needle, has an embedded newline, or uses
   unusual quoting - is handed to Python's `csv` module, which decides where the record
   ends and what its fields are.

So record boundaries and field values always follow the `csv` module's rules as applied
to a file opened with `newline=""` (RFC 4180 style: double-quote quoting, doubled quotes
as escapes; a record ends at LF, CRLF or a bare CR outside quotes). The skipping above
is only used on chunks without bare CRs; a chunk that has one is parsed in full.

Only records that might contain the needle are yielded; the caller does the exact
field comparison. With `needle=None` every record is yielded.
"""

from __future__ import annotations

import csv
import re
import threading
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Iterator

UTF8_BOM = b"\xef\xbb\xbf"

# The csv module's field limit is process-wide. Record length is enforced here instead, per scan
# (max_record_bytes), so lift the module's limit out of the way once rather than juggle it.
csv.field_size_limit(2**31 - 1)


class CsvParseError(Exception):
    """The file could not be parsed reliably from this point on."""

    def __init__(self, message: str, *, record_number: int, byte_offset: int):
        super().__init__(message)
        self.record_number = record_number
        self.byte_offset = byte_offset


@dataclass(slots=True)
class Record:
    number: int  # 1-based ordinal of the record in the file; the header is record 1
    offset: int  # byte offset of the record's first byte
    end: int  # byte offset just past the record
    fields: list[str]


@dataclass
class ScanControl:
    """Shared between a scanning thread and the task supervising it."""

    max_seconds: float | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    # Progress, written by the scanning thread and read by the supervisor.
    started: float | None = None
    total_bytes: int = 0
    bytes_base: int = 0  # bytes of files already finished (multi-file operations)
    bytes_done: int = 0
    matches: int = 0
    phase: str = "queued"
    # Set by iter_records when it stops by itself.
    stop_reason: str | None = None  # end_of_file | cancelled | time_limit
    resume_offset: int = 0
    resume_record: int = 0
    unterminated_quote_at_eof: bool = False
    undecodable_lines: int = 0  # parsed lines holding bytes that are not valid in the file's encoding
    # The search value encoded in encodings other than the declared one, and which of them were seen.
    alt_needles: dict[str, bytes] = field(default_factory=dict)
    alt_hits: set[str] = field(default_factory=set)

    def begin(self) -> None:
        if self.started is None:
            self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return 0.0 if self.started is None else time.monotonic() - self.started

    def interruption(self) -> str | None:
        if self.stop.is_set():
            return "cancelled"
        if self.max_seconds is not None and self.elapsed >= self.max_seconds:
            return "time_limit"
        return None


def prefilter_needle(value: str, encoding: str) -> bytes | None:
    """Bytes that must appear in the raw text of any record holding `value` as a field.

    Quotes and newlines inside a value are written differently in the file (doubled or
    quoted), so the longest fragment free of them is used. Returns None when no safe
    fragment exists; the scan then parses every record.
    """
    fragment = max(re.split(r'["\r\n]', value), key=len)
    if not fragment:
        return None
    try:
        return fragment.encode("utf-8" if encoding == "utf-8-sig" else encoding)
    except UnicodeEncodeError:
        return None


def _is_single_line_record(line: bytes, delimiter: bytes) -> bool:
    """True when `line` is certain to be one complete, cleanly quoted record.

    Pair the line's quotes off in order. The line passes when every opening quote sits at
    the start of a field and every closing quote at the end of one (or is half of a doubled,
    escaped quote). Then the csv module reads the quotes the same way, the record ends at
    the end of the line, and every field's text appears in the line as written - which is
    what makes it safe to skip a line that does not contain the search text. A False result
    does not mean the line is bad, only that the csv module has to parse it.
    """
    parts = line.split(b'"')
    if len(parts) == 1:
        return True
    if not len(parts) & 1:  # odd number of quotes: a quoted field is still open
        return False
    # parts[0], parts[2], ... lie outside quotes: before the first quote, between a closing
    # quote and the next opening one, and after the last quote.
    first, last = parts[0], parts[-1]
    if first and not first.endswith(delimiter):
        return False
    if last and last != b"\r" and not last.startswith(delimiter):
        return False
    for between in set(parts[2:-1:2]):
        if between and not (between.startswith(delimiter) and between.endswith(delimiter)):
            return False
    return True


BARE_CR = re.compile(rb"\r(?!\n)")


class _Lines:
    """Finds physical line ends in one chunk, for feeding csv.reader a record at a time.

    With `universal` set, LF, CRLF and bare CR all end a line (what a text file opened with
    newline="" yields); otherwise only LF does, which is the same thing for a chunk known
    to hold no bare CR. Search positions are cached so a chunk is scanned once, not once
    per line.
    """

    def __init__(self, body: bytes, encoding: str, universal: bool, control: ScanControl):
        self._body = body
        self._encoding = encoding
        self._universal = universal
        self._control = control
        self._lf = self._cr = -2  # next LF / CR at or after the last position asked about
        self.pos = 0
        self.overrun = False  # csv.reader wanted a line beyond those available

    def start(self, pos: int) -> "_Lines":
        self.pos = pos
        self.overrun = False
        return self

    def __iter__(self) -> "_Lines":
        return self

    def __next__(self) -> str:
        body, pos = self._body, self.pos
        if pos >= len(body):
            self.overrun = True
            raise StopIteration
        if self._lf != -1 and self._lf < pos:
            self._lf = body.find(b"\n", pos)
        end = len(body) if self._lf == -1 else self._lf + 1
        if self._universal:
            if self._cr != -1 and self._cr < pos:
                self._cr = body.find(b"\r", pos)
            if self._cr != -1 and self._cr + 1 < end and body[self._cr + 1] != 0x0A:
                end = self._cr + 1  # a bare CR ends the line first
        self.pos = end
        try:
            return body[pos:end].decode(self._encoding)
        except UnicodeDecodeError:
            self._control.undecodable_lines += 1
            return body[pos:end].decode(self._encoding, errors="replace")


def iter_records(
    fh: BinaryIO,
    *,
    delimiter: str = ",",
    encoding: str = "utf-8",
    needle: bytes | None = None,
    start_offset: int = 0,
    start_record: int = 0,
    control: ScanControl | None = None,
    chunk_bytes: int = 4 * 1024 * 1024,
    max_record_bytes: int = 4 * 1024 * 1024,
) -> Iterator[Record]:
    """Yield records from `fh`, starting at a record boundary `start_offset`.

    `start_record` is the number of records that precede `start_offset`. When the
    generator finishes on its own, `control.stop_reason` says why and
    `control.resume_offset` / `control.resume_record` say where a later scan may resume.
    """
    control = control or ScanControl()
    control.begin()
    codec = "utf-8" if encoding == "utf-8-sig" else encoding
    delim = delimiter.encode("ascii")

    fh.seek(start_offset)
    offset = start_offset  # file offset of the first byte not yet turned into a record
    if start_offset == 0:
        if fh.read(len(UTF8_BOM)) == UTF8_BOM:
            offset = len(UTF8_BOM)
        else:
            fh.seek(0)
    rec_no = start_record
    carry = b""
    first_read = True
    read_size = chunk_bytes

    def too_long() -> CsvParseError:
        return CsvParseError(
            f"Record {rec_no + 1} starting at byte {offset} is longer than max_record_bytes "
            f"({max_record_bytes}). This usually means an unterminated quote; everything after "
            "that point cannot be read reliably.",
            record_number=rec_no + 1,
            byte_offset=offset,
        )

    while True:
        control.bytes_done = offset
        chunk = fh.read(read_size)
        read_size = chunk_bytes
        eof = not chunk
        if eof and not carry:
            break  # everything has been read: the scan is complete whatever the clock says
        reason = control.interruption()
        # The time limit is not applied to the first read, so every call makes progress and a
        # chain of cursors always terminates. Cancellation is honoured at once.
        if reason == "cancelled" or (reason and not first_read):
            control.stop_reason, control.resume_offset, control.resume_record = reason, offset, rec_no
            return
        first_read = False
        buf = carry + chunk if carry else chunk
        if eof:
            body, carry = buf, b""
        else:
            # Cut at the last line end. A CR in the final byte is left for the next read,
            # because it may be the first half of a CRLF.
            cut = max(buf.rfind(b"\n"), buf.rfind(b"\r", 0, len(buf) - 1))
            if cut < 0:
                if len(buf) > max_record_bytes:
                    raise too_long()
                carry = buf
                continue
            body, carry = buf[: cut + 1], buf[cut + 1:]

        # The search value's bytes under another encoding only mean something if the chunk itself
        # reads as that encoding: a cp1252 byte also occurs inside valid UTF-8 sequences.
        looks_utf8: bool | None = None
        for name, alt in control.alt_needles.items():
            if name in control.alt_hits or alt not in body:
                continue
            if name == "utf-8" or codec == "utf-8":
                if looks_utf8 is None:
                    try:
                        body.decode("utf-8")
                        looks_utf8 = True
                    except UnicodeDecodeError:
                        looks_utf8 = False
                if (name == "utf-8") != looks_utf8:
                    continue
            control.alt_hits.add(name)

        n = len(body)
        pos = 0
        # A bare CR ends a record, which the skipping below does not allow for: parse the lot.
        skipping = needle is not None and not (b"\r" in body and BARE_CR.search(body))
        lines = _Lines(body, codec, universal=not skipping and b"\r" in body, control=control)
        # Positions of the next needle occurrence and the next quote at or after `pos`:
        # -2 means not looked for yet, -1 means none left in this chunk.
        hit = quote = -2
        pushed_back = False
        parsed = 0
        while pos < n:
            if skipping:
                if hit != -1 and hit < pos:
                    hit = body.find(needle, pos)
                if quote != -1 and quote < pos:
                    quote = body.find(b'"', pos)
                if hit == -1 and quote == -1:
                    # Tier 1: the rest of the chunk is plain lines without the needle.
                    rec_no += body.count(b"\n", pos) + (0 if body.endswith(b"\n") else 1)
                    pos = n
                    break
                interesting = quote if hit == -1 else hit if quote == -1 else min(hit, quote)
                line_start = max(body.rfind(b"\n", pos, interesting) + 1, pos)
                if line_start > pos:
                    # Tier 1: plain lines before the next line of interest.
                    rec_no += body.count(b"\n", pos, line_start)
                    pos = line_start
                eol = body.find(b"\n", pos)
                if eol >= 0 and (hit == -1 or hit > eol) and _is_single_line_record(body[pos:eol], delim):
                    # Tier 2: a complete record that does not contain the needle.
                    rec_no += 1
                    pos = eol + 1
                    continue
            # Tier 3: this record has to be parsed.
            parsed += 1
            if not parsed & 0x3FF:  # a chunk full of candidates takes a while: stay responsive
                reason = control.interruption()
                if reason:
                    control.bytes_done = offset + pos
                    control.stop_reason, control.resume_offset, control.resume_record = reason, offset + pos, rec_no
                    return
            try:
                fields = next(csv.reader(lines.start(pos), delimiter=delimiter), [])
            except csv.Error as exc:
                raise CsvParseError(
                    f"CSV parsing failed at record {rec_no + 1} (byte {offset + pos}): {exc}",
                    record_number=rec_no + 1,
                    byte_offset=offset + pos,
                ) from exc
            if lines.overrun:
                if not eof:
                    # The record continues in the next chunk: re-read it with more data.
                    # Read at least as much again as the record already holds, so a long
                    # record is re-parsed a logarithmic number of times, not once per chunk.
                    offset += pos
                    carry = body[pos:] + carry
                    if len(carry) > max_record_bytes:
                        raise too_long()
                    read_size = max(chunk_bytes, len(carry))
                    pushed_back = True
                    break
                control.unterminated_quote_at_eof = True
            size = lines.pos - pos
            if size > max_record_bytes:
                offset += pos
                raise too_long()
            rec_no += 1
            yield Record(rec_no, offset + pos, offset + pos + size, fields)
            pos += size
        if not pushed_back:
            offset += n

    control.bytes_done = offset
    control.stop_reason, control.resume_offset, control.resume_record = "end_of_file", offset, rec_no
