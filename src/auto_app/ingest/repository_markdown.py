"""Parse the public SpeedyApply and Jobright listing table families.

Source presence is not evidence that an employer is still accepting applications.
Raw table rows belong to ``source_observation``: they preserve relative ages,
yearless dates and source positions without making those volatile values part of
the semantic content fingerprint used by ingestion.
"""

import hashlib
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from auto_app.ingest.models import IngestRecord, ParsedFeed
from auto_app.ingest.urls import canonical_url


_HEADERS = {
    "speedyapply": {
        ("company", "position", "location", "salary", "posting", "age"),
        ("company", "position", "location", "posting", "age"),
    },
    "jobright": {("company", "job title", "location", "work model", "date posted")},
}
_CARRY_FORWARD = {"↳", "↪", "↴"}


class _HTMLCell(HTMLParser):
    def __init__(self, value: str):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[str] = []
        self.feed(value)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "li", "p"}:
            self.text.append("\n")
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)

    def handle_endtag(self, tag):
        if tag in {"li", "p"}:
            self.text.append("\n")

    def handle_data(self, data):
        self.text.append(data)


def _closing(value: str, start: int, opening: str, closing: str) -> int | None:
    depth, escaped = 0, False
    for index in range(start, len(value)):
        char = value[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _markdown_links(value: str):
    """Yield balanced inline links, including labels with parentheses or pipes."""
    cursor = 0
    while cursor < len(value):
        start = value.find("[", cursor)
        if start < 0:
            break
        end = _closing(value, start, "[", "]")
        if end is None:
            break
        if end + 1 < len(value) and value[end + 1] == "(":
            target_end = _closing(value, end + 1, "(", ")")
            if target_end is not None:
                target = value[end + 2:target_end].strip()
                if target.startswith("<") and ">" in target:
                    target = target[1:target.index(">")]
                else:
                    target = target.split(maxsplit=1)[0] if target else ""
                yield start, target_end + 1, value[start + 1:end], target
                cursor = target_end + 1
                continue
        cursor = end + 1


def _plain(value: str) -> str:
    for start, end, label, _ in reversed(list(_markdown_links(value))):
        value = value[:start] + _plain(label) + value[end:]
    value = "".join(_HTMLCell(value).text)
    value = re.sub(r"\\([\\|\[\]()*_`])", r"\1", value)
    return value.replace("**", "").replace("__", "").replace("`", "").strip()


def _links(value: str) -> list[str]:
    return _HTMLCell(value).links + [
        target for start, _, _, target in _markdown_links(value)
        if not start or value[start - 1] != "!"
    ]


def _cells(line: str) -> list[str] | None:
    line = line.strip()
    if not line.startswith("|"):
        return None
    # Some Jobright company link labels contain an unescaped literal pipe.
    # Preserve these labels, escaped pipes, and pipes inside HTML attributes.
    cells, start, brackets, parentheses = [], 1, 0, 0
    in_tag, quote, escaped, previous = False, None, False, ""
    for index, char in enumerate(line[1:], 1):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif in_tag:
            if char == quote:
                quote = None
            elif char in {"'", '"'} and quote is None:
                quote = char
            elif char == ">" and quote is None:
                in_tag = False
        elif char == "<" and index + 1 < len(line) and (line[index + 1].isalpha() or line[index + 1] == "/"):
            in_tag = True
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets = max(0, brackets - 1)
        elif char == "(" and (previous == "]" or parentheses):
            parentheses += 1
        elif char == ")" and parentheses:
            parentheses -= 1
        elif char == "|" and not brackets and not parentheses:
            cells.append(line[start:index].strip())
            start = index + 1
        previous = char
    if start < len(line):
        cells.append(line[start:].strip())
    return cells


def _locations(value: str) -> list[str]:
    # A details summary is a count/label, not another observed location.
    value = re.sub(r"<summary\b[^>]*>.*?</summary>", "", value, flags=re.I | re.S)
    return sorted({part.strip() for part in _plain(value).splitlines() if part.strip()})


def _company(value: str, previous: tuple[str, list[str]] | None):
    name = " ".join(_plain(value).split())
    if name in _CARRY_FORWARD:
        if previous is None:
            raise ValueError("company carry-forward has no preceding company in this table")
        return previous
    if not name:
        raise ValueError("company must be nonempty")
    urls = sorted({url for raw in _links(value) if (url := canonical_url(raw))})
    return name, urls


def _record(row: dict[str, str], company: tuple[str, list[str]], provider: str,
            raw: str, line: int) -> IngestRecord:
    title_column, link_column = ("position", "posting") if provider == "speedyapply" else ("job title", "job title")
    title = " ".join(_plain(row[title_column]).split())
    if not title:
        raise ValueError("title must be nonempty")
    urls = {url for raw_url in _links(row[link_column]) if (url := canonical_url(raw_url))}
    if len(urls) != 1:
        raise ValueError("job link must contain exactly one absolute HTTP(S) URL")
    url = urls.pop()
    if provider == "jobright":
        parts = urlsplit(url)
        match = re.fullmatch(r"/jobs/info/([A-Za-z0-9_-]+)", parts.path)
        if parts.hostname not in {"jobright.ai", "www.jobright.ai"} or not match:
            raise ValueError("job link must identify a Jobright job detail page")
        external_id = "jobright:" + match.group(1)
    else:
        external_id = "url:" + hashlib.sha256(url.encode()).hexdigest()
    return IngestRecord(
        external_id=external_id,
        url=url,
        title=title,
        company=company[0],
        locations=_locations(row["location"]),
        # Neither source's table supplies a year or explicit employer status.
        active=None,
        metadata={
            "format": provider,
            "upstream": {
                "company_urls": company[1],
                "company_variants": [company[0]],
                "title_variants": [title],
                "salaries": [_plain(row["salary"])] if row.get("salary") else [],
                "work_models": [_plain(row["work model"])] if row.get("work model") else [],
            },
            "source_observation": {"rows": [{"line": line, "raw": raw.strip(), "cells": row}]},
        },
    )


def _merge(existing: IngestRecord, incoming: IngestRecord) -> None:
    existing.metadata["source_observation"]["rows"].extend(incoming.metadata["source_observation"]["rows"])
    existing.locations = sorted(set(existing.locations) | set(incoming.locations))
    for key, values in incoming.metadata["upstream"].items():
        existing.metadata["upstream"][key] = sorted(set(existing.metadata["upstream"][key]) | set(values))
    # Exact application identity can appear with different source labels (for
    # example, IDeaS and SAS). Keep every label, choosing an observed pair in a
    # deterministic order so table reordering does not manufacture an update.
    existing.company, existing.title = min(
        (existing.company, existing.title), (incoming.company, incoming.title),
    )


def _parse(content: bytes, provider: str) -> ParsedFeed:
    try:
        lines = content.decode("utf-8-sig").splitlines()
    except UnicodeError as exc:
        raise ValueError("repository Markdown feed must be UTF-8") from exc
    result, records = ParsedFeed(), {}
    columns, previous_company = None, None
    found_table, row_index, skip_separator = False, 0, False
    fence = None
    for offset, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else (marker if fence is None else fence)
            columns, previous_company = None, None
            continue
        if fence:
            continue
        cells = _cells(line)
        if skip_separator:
            skip_separator = False
            continue
        if cells is None:
            if columns is not None and stripped and not stripped.startswith(("#", "<!--", "<a name=")):
                result.issues.append({
                    "index": row_index, "line": offset + 1,
                    "reason": "unrecognized content inside listing table",
                })
                row_index += 1
                previous_company = None
                continue
            columns, previous_company = None, None
            continue
        header = tuple(_plain(cell).lower() for cell in cells)
        if header in _HEADERS[provider]:
            separator = _cells(lines[offset + 1]) if offset + 1 < len(lines) else None
            if separator is None or len(separator) != len(header) or not all(
                re.fullmatch(r":?-{3,}:?", cell) for cell in separator
            ):
                raise ValueError(f"repository listing table has no valid delimiter at line {offset + 1}")
            found_table, skip_separator = True, True
            columns, previous_company = header, None
            continue
        if "company" in header and ("position" in header or "job title" in header):
            raise ValueError(f"unsupported repository listing columns at line {offset + 1}")
        if columns is None:
            continue
        try:
            if len(cells) != len(columns):
                previous_company = None
                raise ValueError(f"listing row has {len(cells)} columns; expected {len(columns)}")
            row = dict(zip(columns, cells))
            try:
                previous_company = _company(row["company"], previous_company)
            except ValueError:
                previous_company = None
                raise
            record = _record(row, previous_company, provider, line, offset + 1)
            if record.external_id in records:
                _merge(records[record.external_id], record)
            else:
                records[record.external_id] = record
                result.records.append(record)
        except ValueError as exc:
            result.issues.append({"index": row_index, "line": offset + 1, "reason": str(exc)})
        row_index += 1
    if not found_table:
        raise ValueError(f"repository feed has no supported {provider} listing table")
    return result


def parse_speedyapply(content: bytes) -> ParsedFeed:
    """Parse all SpeedyApply listing tables, with or without salary columns."""
    return _parse(content, "speedyapply")


def parse_jobright(content: bytes) -> ParsedFeed:
    """Parse Jobright's rolling public table without guessing date years."""
    return _parse(content, "jobright")
