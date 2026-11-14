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

So record boundaries and field values always follow the `csv` module's rules
(RFC 4180 style: double-quote quoting, doubled quotes as escapes, LF or CRLF line ends).

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
    """True when `line` is certain to be one complete record under the csv module's rules.

    The csv module opens a quoted field only when a quote is the first character of a field.
    Pair the line's quotes off in order: if every opening quote sits at the start of the
    line, straight after a delimiter, or straight after the previous closing quote (an
    escaped quote), the csv module reads the quotes the same way and cannot be inside a
    quoted field at the end of the line, so the record ends there. A False result does not
    mean the line is bad, only that the csv module has to decide.
    """
    parts = line.split(b'"')
    if not len(parts) & 1:  # odd number of quotes: a quoted field is still open
        return False
    # parts[0], parts[2], ... are the stretches that precede each opening quote.
    for before in set(parts[0:-1:2]):
        if before and not before.endswith(delimiter):
            return False
    return True


class _BodyFeeder:
    """Feeds physical lines to csv.reader and records how far it read."""

    def __init__(self, body: bytes, pos: int, encoding: str):
        self._body = body
        self._encoding = encoding
        self.pos = pos
        self.overrun = False  # csv.reader wanted a line beyond those available

    def __iter__(self) -> "_BodyFeeder":
        return self

    def __next__(self) -> str:
        body = self._body
        if self.pos >= len(body):
            self.overrun = True
            raise StopIteration
        eol = body.find(b"\n", self.pos)
        end = len(body) if eol < 0 else eol + 1
        line = body[self.pos: end]
        self.pos = end
        return line.decode(self._encoding, errors="replace")


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
    max_record_bytes: int = 16 * 1024 * 1024,
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
    csv.field_size_limit(max(csv.field_size_limit(), max_record_bytes))

    fh.seek(start_offset)
    offset = start_offset  # file offset of the first byte not yet turned into a record
    if start_offset == 0:
        if fh.read(len(UTF8_BOM)) == UTF8_BOM:
            offset = len(UTF8_BOM)
        else:
            fh.seek(0)
    rec_no = start_record
    carry = b""

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
        reason = control.interruption()
        if reason:
            control.stop_reason, control.resume_offset, control.resume_record = reason, offset, rec_no
            return

        chunk = fh.read(chunk_bytes)
        eof = not chunk
        buf = carry + chunk if carry else chunk
        if eof:
            body, carry = buf, b""
            if not body:
                break
        else:
            cut = buf.rfind(b"\n")
            if cut < 0:
                if len(buf) > max_record_bytes:
                    raise too_long()
                carry = buf
                continue
            body, carry = buf[: cut + 1], buf[cut + 1:]

        n = len(body)
        pos = 0
        # Positions of the next needle occurrence and the next quote at or after `pos`:
        # -2 means not looked for yet, -1 means none left in this chunk.
        hit = quote = -2
        pushed_back = False
        while pos < n:
            if needle is not None:
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
            eol = body.find(b"\n", pos)
            line = body[pos: n if eol < 0 else eol]
            if b'"' not in line:
                size = len(line) + (0 if eol < 0 else 1)
                if line.endswith(b"\r"):
                    line = line[:-1]
                fields = line.decode(codec, errors="replace").split(delimiter) if line else []
            else:
                feeder = _BodyFeeder(body, pos, codec)
                try:
                    fields = next(csv.reader(feeder, delimiter=delimiter), [])
                except csv.Error as exc:
                    raise CsvParseError(
                        f"CSV parsing failed at record {rec_no + 1} (byte {offset + pos}): {exc}",
                        record_number=rec_no + 1,
                        byte_offset=offset + pos,
                    ) from exc
                if feeder.overrun:
                    if not eof:
                        # The record continues in the next chunk: re-read it with more data.
                        offset += pos
                        carry = body[pos:] + carry
                        if len(carry) > max_record_bytes:
                            raise too_long()
                        pushed_back = True
                        break
                    control.unterminated_quote_at_eof = True
                size = feeder.pos - pos
            rec_no += 1
            yield Record(rec_no, offset + pos, offset + pos + size, fields)
            pos += size
        if not pushed_back:
            offset += n

    control.bytes_done = offset
    control.stop_reason, control.resume_offset, control.resume_record = "end_of_file", offset, rec_no
