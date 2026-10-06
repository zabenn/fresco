"""The extraction pipeline: PDF bytes in, hardware sets out, with page locations,
resolved codes and a confidence level for every field.

    read_pages -> find_hardware_pages -> group_pages -> model requests (in parallel)
    -> join sets split between requests -> check every field against the PDF text
    -> resolve codes with the book's own code tables and codes.json
"""

import asyncio
import json
import re
from collections import Counter, defaultdict
from enum import StrEnum
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

from fresco.llm import LLM, ModelAnswer, ModelCode, ModelComponent, ModelSet
from fresco.pdf import (
    SET_HEADER_RE,
    Line,
    find_hardware_pages,
    group_pages,
    read_pages,
    render_page,
    set_header_lines,
)

MAX_PARALLEL_REQUESTS = 8
AMBIGUOUS_CODES = {"PE", "NO"}  # Pemko or painted enamel; Norton or "No."


class Confidence(StrEnum):
    FOUND = "found"  # in the cited lines and passed the checks (not proof it is right)
    AMBIGUOUS = "ambiguous"  # an ambiguous code, placed by its column
    SUSPECT = "suspect"  # failed a check; see the component's warnings


class Location(BaseModel):
    """A box on a page, in points from the page's top-left, like Word and Line:
    x = (left, right), y = (top, bottom)."""

    page: int
    x: tuple[float, float]
    y: tuple[float, float]


class BookCode(BaseModel):
    """An entry in the book's own code table: ("option", "EC2", "Flush Endcap")."""

    kind: Literal["mfr", "finish", "option"]
    code: str
    meaning: str
    location: Location | None


class Component(BaseModel):
    qty: float | None
    unit: str | None
    description: str
    catalog_number: str | None
    mfr: str | None
    finish: str | None
    notes: str | None
    mfr_normalized: str | None
    finish_normalized: str | None  # BHMA number
    confidence: dict[str, Confidence]
    warnings: list[str]


class HardwareSet(BaseModel):
    set_number: str | None
    description: str | None
    not_used: bool
    openings: list[str]
    location: list[Location]  # one per page the set covers
    components: list[Component]
    notes: list[str]
    operation: str | None
    warnings: list[str]


class ExtractionResult(BaseModel):
    filename: str | None
    page_count: int
    hardware_pages: list[int]
    sets: list[HardwareSet]
    code_tables: list[BookCode]  # the book's own manufacturer, finish and option codes
    # Printed codes that neither the book's tables nor codes.json explain.
    unresolved_codes: dict[str, list[str]]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    batch_tokens: dict[str, int] = {}  # batch mode only; billed at half price
    warnings: list[str]
    pdf_hash: str | None = None  # set by the API, which stores each PDF by its hash
    corrected: bool = False  # True when this is a stored, hand-corrected result


# The book's code tables by kind ("mfr", "finish", "option"), each keyed by
# _compare_key(code).
CodeTables = dict[str, dict[str, BookCode]]


def _drop_or_equal(text: str) -> str:
    """ "Ives or equal" -> "Ives"."""
    return re.sub(r"\s+or\s+equal\b.*", "", text, flags=re.IGNORECASE).strip()


def _compare_key(text: str) -> str:
    """Uppercase letters and digits only, without "or equal": "Ives or equal" -> "IVES"."""
    return re.sub(r"[^A-Z0-9]", "", _drop_or_equal(text).upper())


_codes = json.loads((Path(__file__).parent / "codes.json").read_text())
# Every alias keyed to one canonical value: {"IVE": "Ives", "IVES": "Ives", ...} and
# {"US26D": "626", "C26D": "626", ...}.
MANUFACTURER_ALIASES = {
    _compare_key(alias): name
    for name, aliases in _codes["manufacturers"].items()
    for alias in [name, *aliases]
}
FINISH_ALIASES = {
    _compare_key(alias): bhma
    for bhma, aliases in _codes["finishes"].items()
    for alias in [bhma, *aliases]
}


def normalize_mfr(value: str) -> str | None:
    return MANUFACTURER_ALIASES.get(_compare_key(value))


def _normalize_mfr_name(name: str) -> str:
    """A manufacturer name from a book's code table, matched in full or by its first
    word: "LCN Closers or equal" -> "LCN", "Hager Companies" -> "Hager"; kept as
    printed (without "or equal") if neither is in codes.json."""
    first_word = name.split()[0] if name.split() else ""
    return normalize_mfr(name) or normalize_mfr(first_word) or _drop_or_equal(name)


def normalize_finish(value: str) -> str | None:
    """ "US26D" -> "626"; a split finish like "626/622" is mapped part by part."""
    parts = [FINISH_ALIASES.get(_compare_key(part)) for part in value.split("/")]
    return None if None in parts else "/".join(parts)


def looks_like_finish(value: str) -> bool:
    return bool(normalize_finish(value) or re.fullmatch(r"\d{3}[A-Z]?", value.strip()))


def _in_source(value: str, source: str) -> bool:
    """Every word of value appears in source (a _compare_key of the cited text).

    Checked word by word because a value can wrap onto the next line, with other
    columns' text in between.
    """
    words = [_compare_key(word) for word in value.split() if _compare_key(word)]
    return bool(words) and all(word in source for word in words)


def _check_component(
    raw: ModelComponent, cited: list[Line], tables: CodeTables
) -> Component:
    """Check the model's component against the lines it cited and resolve its codes."""
    source = _compare_key(" ".join(line.text for line in cited))
    mfr_entry = tables["mfr"].get(_compare_key(raw.mfr))
    confidence: dict[str, Confidence] = {}
    warnings: list[str] = []

    def suspect(field: str, reason: str) -> None:
        confidence[field] = Confidence.SUSPECT
        warnings.append(f"{field}: {reason}")

    if not cited:
        warnings.append("no valid source lines cited")
    if raw.qty is not None:
        confidence["qty"] = Confidence.FOUND
        quantity_at_start = re.compile(rf"\s*0*{raw.qty:g}(\.0+)?\b")
        if not any(quantity_at_start.match(line.text) for line in cited):
            suspect("qty", f"{raw.qty:g} is not at the start of its line")

    for field in ("description", "catalog_number", "notes", "mfr", "finish"):
        value = getattr(raw, field)
        if not value:
            continue
        confidence[field] = Confidence.FOUND
        key = _compare_key(value)
        is_mfr = bool(normalize_mfr(value)) or key in tables["mfr"]
        is_finish = looks_like_finish(value) or key in tables["finish"]
        if not _in_source(value, source):
            suspect(field, f"{value!r} not found in the PDF text")
        # Codes missing from codes.json are fine; only a value that is clearly the
        # other kind of code is suspect.
        elif field == "mfr" and is_finish and not is_mfr:
            suspect(field, f"{value!r} looks like a finish code")
        elif field == "finish" and is_mfr and not is_finish:
            suspect(field, f"{value!r} looks like a manufacturer code")
        # "PE" is only ambiguous if the book doesn't define it for this field.
        elif key in AMBIGUOUS_CODES and key not in tables[field]:
            confidence[field] = Confidence.AMBIGUOUS

    # Fields the model left out arrive as "" and are output as null.
    fields = {
        name: value if value != "" else None
        for name, value in raw.model_dump(exclude={"line_ids"}).items()
    }
    # The book's own table wins for manufacturers: its "PE" or "HA" means what it says.
    if mfr_entry:
        mfr_normalized: str | None = _normalize_mfr_name(mfr_entry.meaning)
    else:
        mfr_normalized = normalize_mfr(raw.mfr) if raw.mfr else None
    return Component(
        **fields,
        mfr_normalized=mfr_normalized,
        finish_normalized=normalize_finish(raw.finish) if raw.finish else None,
        confidence=confidence,
        warnings=warnings,
    )


def _code_tables(
    model_codes: list[ModelCode], lines: dict[str, Line]
) -> tuple[list[BookCode], CodeTables, list[str]]:
    """The book's code tables from every answer. A code defined twice with different
    meanings keeps the first and gets a warning."""
    entries: list[BookCode] = []
    tables: CodeTables = {"mfr": {}, "finish": {}, "option": {}}
    warnings = []
    for model_code in model_codes:
        key = _compare_key(model_code.code)
        if not key:
            continue
        if existing := tables[model_code.kind].get(key):
            if _compare_key(existing.meaning) != _compare_key(model_code.meaning):
                warnings.append(
                    f"{model_code.kind} code {model_code.code!r} is defined as both "
                    f"{existing.meaning!r} and {model_code.meaning!r}; using the first"
                )
            continue
        cited = [lines[line_id] for line_id in model_code.line_ids if line_id in lines]
        entry = BookCode(
            kind=model_code.kind,
            code=model_code.code,
            meaning=model_code.meaning,
            location=next(iter(_locations(cited)), None),
        )
        tables[model_code.kind][key] = entry
        entries.append(entry)
    return entries, tables, warnings


def _join_split_sets(
    results: list[tuple[list[int], ModelAnswer | None]], lines: dict[str, Line]
) -> tuple[list[ModelSet], list[str]]:
    """Join sets that were split between two requests back into one.

    results holds (page group, answer) per request in page order; the answer is None
    if the request failed. The first set of an answer is joined to the last set of
    the previous answer when the model marked it as a continuation or repeated the
    set number, but only if the previous request succeeded and ended on the page
    before; otherwise the rows could land in the wrong set.
    """
    joined: list[ModelSet] = []
    warnings: list[str] = []
    previous_pages: list[int] | None = None
    previous_ok = False
    for pages, answer in results:
        for index, model_set in enumerate(answer.sets if answer else []):
            last_number = joined[-1].set_number if joined else None
            # A "continuation" that cites a set header or carries its own set number
            # is really a new set (the model sometimes lumps the next set in).
            header = _cited_set_number(model_set, lines)
            if header and header != last_number:
                model_set.continues_previous_set = False
                model_set.set_number = model_set.set_number or header
            if model_set.continues_previous_set and model_set.set_number:
                model_set.continues_previous_set = model_set.set_number == last_number
            same_number = model_set.set_number and model_set.set_number == last_number
            if (
                index == 0
                and joined
                and (model_set.continues_previous_set or same_number)
            ):
                follows = (
                    previous_pages is not None and previous_pages[-1] == pages[0] - 1
                )
                if previous_ok and follows:
                    _append_continuation(joined[-1], model_set)
                    continue
                warnings.append(
                    f"page {pages[0]} continues a set from a page that wasn't "
                    "extracted; kept as a separate set without a number"
                )
            joined.append(model_set)
        previous_pages, previous_ok = pages, answer is not None
    return joined, warnings


def _append_continuation(model_set: ModelSet, continuation: ModelSet) -> None:
    model_set.components += continuation.components
    model_set.header_line_ids += continuation.header_line_ids
    model_set.openings += continuation.openings
    model_set.notes += continuation.notes
    model_set.note_line_ids += continuation.note_line_ids
    if continuation.operation:
        model_set.operation = " ".join(
            filter(None, [model_set.operation, continuation.operation])
        )
    model_set.operation_line_ids += continuation.operation_line_ids


def _cited_set_number(model_set: ModelSet, lines: dict[str, Line]) -> str | None:
    """The set number from a set header line the set cites, e.g. "CY207"."""
    for line_id in model_set.header_line_ids:
        if line_id in lines and (match := SET_HEADER_RE.match(lines[line_id].text)):
            return match.group(1)
    return None


def _locations(cited: list[Line]) -> list[Location]:
    """The box around the cited lines on each page they're on."""
    lines_by_page: dict[int, list[Line]] = defaultdict(list)
    for line in cited:
        lines_by_page[line.page].append(line)
    return [
        Location(
            page=page,
            x=(
                round(min(line.x[0] for line in page_lines), 1),
                round(max(line.x[1] for line in page_lines), 1),
            ),
            y=(
                round(min(line.y[0] for line in page_lines), 1),
                round(max(line.y[1] for line in page_lines), 1),
            ),
        )
        for page, page_lines in sorted(lines_by_page.items())
    ]


def _build_set(
    raw: ModelSet, lines: dict[str, Line], tables: CodeTables
) -> HardwareSet:
    """Turn the model's set into the output set: locations, checks, normalized codes."""

    def cited(line_ids: list[str]) -> list[Line]:
        return [lines[line_id] for line_id in line_ids if line_id in lines]

    all_line_ids = (
        raw.header_line_ids
        + [line_id for component in raw.components for line_id in component.line_ids]
        + raw.note_line_ids
        + raw.operation_line_ids
    )
    set_level_text = [
        ("opening", opening, raw.header_line_ids) for opening in raw.openings
    ]
    set_level_text += [("note", note, raw.note_line_ids) for note in raw.notes]
    if raw.operation:
        set_level_text.append(("operation", raw.operation, raw.operation_line_ids))
    warnings = [
        f"{kind} {value[:60]!r} not found in the PDF text"
        for kind, value, line_ids in set_level_text
        if not _in_source(
            value, _compare_key(" ".join(line.text for line in cited(line_ids)))
        )
    ]
    return HardwareSet(
        set_number=raw.set_number,
        description=raw.description or None,
        not_used=raw.not_used,
        openings=raw.openings,
        location=_locations(cited(list(dict.fromkeys(all_line_ids)))),
        components=[
            _check_component(component, cited(component.line_ids), tables)
            for component in raw.components
        ],
        notes=raw.notes,
        operation=raw.operation or None,
        warnings=warnings,
    )


def _request_text(pages: list[list[Line]], page_group: list[int]) -> str:
    return "\n".join(
        f"=== page {page_number} ===\n" + render_page(pages[page_number - 1])
        for page_number in page_group
    )


def request_texts(data: bytes) -> list[str]:
    """The text of every request extract() would send for this PDF."""
    pages = read_pages(data)
    page_groups = group_pages(pages, find_hardware_pages(pages))
    return [_request_text(pages, page_group) for page_group in page_groups]


async def extract(data: bytes, filename: str | None, llm: LLM) -> ExtractionResult:
    """Extract every hardware set in the PDF, calling the model live."""
    pages = await asyncio.to_thread(read_pages, data)
    hardware_pages = find_hardware_pages(pages)
    lines = {line.id: line for page in pages for line in page}
    tokens: Counter[str] = Counter()
    warnings: list[str] = []
    parallel_limit = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)
    # Anthropic's prompt cache only serves the shared prompt once a response that
    # wrote it has started streaming; requests sent at the same moment would each pay
    # full price for it. So the first request goes alone and the rest wait for it.
    first_started = asyncio.Event()

    async def run(
        page_group: list[int], first: bool
    ) -> tuple[list[int], ModelAnswer | None]:
        if not first:
            await first_started.wait()
        async with parallel_limit:
            try:
                answer, request_tokens = await llm.extract_sets(
                    _request_text(pages, page_group), first_started if first else None
                )
            except (ValueError, anthropic.APIConnectionError, anthropic.RateLimitError,
                    anthropic.InternalServerError) as error:  # fmt: skip
                warnings.append(
                    f"pages {page_group[0]}-{page_group[-1]} failed: {error}"
                )
                return page_group, None
            finally:
                if first:  # release the others even after a cache hit or an error
                    first_started.set()
        tokens.update(request_tokens)
        return page_group, answer

    page_groups = group_pages(pages, hardware_pages)
    results = await asyncio.gather(
        *(run(page_group, index == 0) for index, page_group in enumerate(page_groups))
    )
    model_sets, join_warnings = _join_split_sets(results, lines)
    warnings += join_warnings
    code_tables, tables, code_warnings = _code_tables(
        [code for _, answer in results if answer for code in answer.codes], lines
    )
    warnings += code_warnings
    sets = [_build_set(model_set, lines, tables) for model_set in model_sets]

    # Each set header on the pages should give one set; a mismatch means sets were
    # missed, invented or wrongly joined.
    header_count = sum(
        len(set_header_lines(pages[page_number - 1])) for page_number in hardware_pages
    )
    if header_count and len(sets) != header_count:
        warnings.append(
            f"extracted {len(sets)} sets but found {header_count} set headers on the pages"
        )

    unresolved: dict[str, set[str]] = {"mfr": set(), "finish": set()}
    for component in (
        component for hardware_set in sets for component in hardware_set.components
    ):
        if component.mfr and not component.mfr_normalized:
            unresolved["mfr"].add(component.mfr)
        if (
            component.finish
            and not component.finish_normalized
            and _compare_key(component.finish) not in tables["finish"]
        ):
            unresolved["finish"].add(component.finish)

    return ExtractionResult(
        filename=filename,
        page_count=len(pages),
        hardware_pages=hardware_pages,
        sets=sets,
        code_tables=code_tables,
        unresolved_codes={field: sorted(codes) for field, codes in unresolved.items()},
        input_tokens=tokens["input"],
        output_tokens=tokens["output"],
        cache_read_tokens=tokens["cache_read"],
        cache_write_tokens=tokens["cache_write"],
        warnings=warnings,
    )


async def extract_batch(
    data: bytes, filename: str | None, llm: LLM
) -> ExtractionResult | None:
    """Batch mode, one step per call: call again with the same PDF until it returns
    a result.

    The first call sends the uncached requests to Anthropic's Batch API (half price,
    usually done within the hour) and returns None, as do calls while the batch runs.
    Once it has ended, its answers are cached and extract() builds the result from
    them, retrying live any request that failed in the batch.
    """
    texts = await asyncio.to_thread(request_texts, data)
    batch_key = llm.batch_key(texts)
    batch = llm.database.get_batch(batch_key)
    if batch is None:
        batch_id = await llm.submit_batch(texts)
        if batch_id is None:  # every request is already cached
            return await extract(data, filename, llm)
        llm.database.add_batch(batch_key, batch_id)
        return None
    batch_id, batch_tokens = batch
    if batch_tokens is None:
        batch_tokens = await llm.collect_batch(batch_id)
        if batch_tokens is None:
            return None
        llm.database.finish_batch(batch_key, batch_tokens)
    result = await extract(data, filename, llm)
    result.batch_tokens = batch_tokens
    return result
