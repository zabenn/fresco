"""Read a PDF into numbered lines of cells, pick the pages with hardware sets, and group
them into model requests."""

import itertools
import re
from dataclasses import dataclass

import pymupdf

# Measurements are in PDF points (1/72 inch).
SAME_LINE_TOLERANCE = 3.0  # words whose tops are this close share a line
CELL_GAP = 12.0  # a wider horizontal gap between words starts a new cell
BLOCK_GAP_FACTOR = 3  # a vertical gap over 3x the page's median line gap starts a block
HEADER_FOOTER_MARGIN = 0.1  # share of the page height at the top and bottom
HEADER_FOOTER_MIN_PAGES = 3  # a margin line repeated on this many pages is removed
MIN_ITEM_ROWS = 3  # item rows that make a page a hardware page on their own
CODE_TABLE_REACH = 3  # a code table this many pages from a hardware page is sent too
PAGES_PER_REQUEST = 3  # target pages per model request...
MAX_PAGES_PER_REQUEST = 5  # ...stretched up to this to keep a set in one request

# An item row starts with a quantity ("3", "1.5", "2 EA") and then text, so a
# numbered list item ("1. Hinges: ...") or a spec article ("2.02 HINGES") doesn't match.
QUANTITY_START_RE = re.compile(
    r"^\d+(?:\.\d)?\s+(?:EA|PR|SET|SETS|EACH|PAIR|LF|PC|PCS)?\.?\s*[A-Za-z]",
    re.IGNORECASE,
)
HARDWARE_WORD_RE = re.compile(
    r"hinge|butt|pivot|closer|lock|latch|exit|panic|cylinder|core|stop|holder|"
    r"silencer|mute|threshold|sweep|seal|gasket|kick|plate|pull|push|bolt|strike|"
    r"coordinator|operator|actuator|astragal|drip|viewer|power transfer|switch|"
    r"lever|deadbolt|weatherstrip|bumper|overhead|key|bracket|armor",
    re.IGNORECASE,
)
# "set" / "group" / "heading" / "HW" and a number anywhere in a line. Loose: it also matches
# "Hardware Group/Set #103 on ..." or "PIVOT SET 7226", so it is only used where
# over-matching is safe (choosing pages, protecting lines from header removal).
SET_MENTION_RE = re.compile(
    r"\b(?:hardware\s+)?(?:set|group|heading|hw)\b.{0,20}\d", re.IGNORECASE
)
# A set header that starts its line, e.g. "Set #01", "Hardware Group No. 087",
# "PART 30 - HARDWARE GROUP NO. 401", "Set: EX-1.0", "HW 04A". Group 1 is the set
# number.
SET_HEADER_RE = re.compile(
    r"^(?:PART\s+\d+\s*-\s*)?(?:hardware\s+)?(?:sets?|groups?|heading|hw)(?:\s*/\s*set)?"
    r"\s*(?:no\.?|#|:)?\s*#?\s*([A-Z]{0,4}[-.]?\d[\w.-]*)",
    re.IGNORECASE,
)
# The heading of a book's own code table, e.g. "C. Manufacturer List", "D. Option
# List", "B. Manufacturer’s Abbreviations:", "D. Hardware Finish List".
CODE_TABLE_RE = re.compile(
    r"^(?:[A-Z0-9]{1,2}\.\s*)?(?:hardware\s+)?(?:manufacturer[’']?s?[’']?|finish(?:es)?|options?)\s+"
    r"(?:legend|list|abbreviations|key)\s*:?\s*$",
    re.IGNORECASE,
)
NOT_USED_RE = re.compile(r"\bnot\s+used\b|\bN/A\b", re.IGNORECASE)
ICON_GLYPH_RE = re.compile(r"[-]+")  # private-use characters, e.g. ""


@dataclass
class Word:
    """A word and its extent: x = (left, right), y = (top, bottom).

    Word(x=(136.2, 150.8), y=(303.6, 314.5), text="BB")
    """

    x: tuple[float, float]
    y: tuple[float, float]
    text: str


@dataclass
class Cell:
    """Words on one line with no wide gap between them, e.g. a table cell.

    Cell(x=(136.2, 188.3), text="BB HINGE")
    """

    x: tuple[float, float]
    text: str


@dataclass
class Line:
    """One visual line of a page.

    Line(id="p7.15", page=7, block=5, x=(84.0, 283.6), y=(303.6, 314.5),
         cells=[Cell((84.0, 116.1), "3 EA"), Cell((136.2, 188.3), "BB HINGE"),
                Cell((263.0, 283.6), "BLK")])

    The id ("p<page>.<line>") is what the model cites. Lines in the same block are
    not separated by extra whitespace: a set header, a table and a notes paragraph
    usually get different blocks.
    """

    id: str
    page: int
    block: int
    x: tuple[float, float]
    y: tuple[float, float]
    cells: list[Cell]

    @property
    def text(self) -> str:
        """The plain text: "3 EA BB HINGE BLK"."""
        return " ".join(cell.text for cell in self.cells)

    def render(self) -> str:
        """The line as the model reads it: "p7.15 | [84] 3 EA | [136] BB HINGE | ...".

        Each cell starts with its left edge, so cells at the same x across rows read
        as one column.
        """
        return f"{self.id} | " + " | ".join(
            f"[{cell.x[0]:.0f}] {cell.text}" for cell in self.cells
        )


def read_pages(data: bytes) -> list[list[Line]]:
    """Read every page into lines; pages[0] is page 1.

    Line ids count from 1 ("p7.1", "p7.2", ...) and skip removed headers and
    footers. Scanned pages without a text layer come back empty.
    """
    with pymupdf.open(stream=data, filetype="pdf") as document:
        pages = [
            _page_lines(page, page_number)
            for page_number, page in enumerate(document, start=1)
        ]
        heights = [page.rect.height for page in document]
    _remove_headers_and_footers(pages, heights)
    return pages


def _page_lines(page: pymupdf.Page, page_number: int) -> list[Line]:
    words = sorted(
        _horizontal_words(page), key=lambda word: (round(word.y[0]), word.x[0])
    )
    rows: list[list[Word]] = []
    for word in words:
        if rows and abs(rows[-1][0].y[0] - word.y[0]) <= SAME_LINE_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])

    # A new block starts where the gap above a row is much wider than this page's
    # usual line spacing: rows and wrapped text sit 1-3pt apart, while a table is set
    # off from the header above and the notes below by 6-25pt.
    tops = [min(word.y[0] for word in row) for row in rows]
    bottoms = [max(word.y[1] for word in row) for row in rows]
    gaps = [top - bottom for bottom, top in zip(bottoms, tops[1:])]
    positive_gaps = sorted(gap for gap in gaps if gap > 0)
    block_gap = (
        BLOCK_GAP_FACTOR * positive_gaps[len(positive_gaps) // 2] + 2
        if positive_gaps
        else float("inf")
    )

    lines = []
    block = 0
    for index, row in enumerate(rows):
        if index and gaps[index - 1] > block_gap:
            block += 1
        row.sort(key=lambda word: word.x[0])
        cells = [[row[0]]]
        for previous, word in itertools.pairwise(row):
            if word.x[0] - previous.x[1] > CELL_GAP:
                cells.append([])
            cells[-1].append(word)
        lines.append(
            Line(
                id=f"p{page_number}.{index + 1}",
                page=page_number,
                block=block,
                x=(min(word.x[0] for word in row), max(word.x[1] for word in row)),
                y=(tops[index], bottoms[index]),
                cells=[
                    Cell(
                        (cell[0].x[0], cell[-1].x[1]),
                        " ".join(word.text for word in cell),
                    )
                    for cell in cells
                ],
            )
        )
    return lines


def _horizontal_words(page: pymupdf.Page) -> list[Word]:
    """The page's words, without sideways text or icon glyphs.

    Sideways text, such as a copyright notice printed up the margin, would otherwise
    land in the table rows beside it. PyMuPDF numbers each word by (block, line) and
    records each line's direction, which is how sideways lines are found.
    """
    textpage = page.get_textpage(flags=pymupdf.TEXTFLAGS_WORDS)
    layout = page.get_text("dict", textpage=textpage)
    horizontal_lines = {
        (block["number"], line_index)
        for block in layout["blocks"]
        if block["type"] == 0
        for line_index, line in enumerate(block["lines"])
        if line["dir"] == (1.0, 0.0)
    }
    return [
        Word((x0, x1), (y0, y1), text)
        for x0, y0, x1, y1, text, block, line, _ in page.get_text(
            "words", textpage=textpage
        )
        if (block, line) in horizontal_lines and not ICON_GLYPH_RE.fullmatch(text)
    ]


def _remove_headers_and_footers(pages: list[list[Line]], heights: list[float]) -> None:
    """Remove lines repeated near the top or bottom of several pages, such as
    "DOOR HARDWARE 08 71 00 - 34" or "Page 27 of 238".

    Digits are masked so changing page numbers still match. Only blocks lying wholly
    in the margin count, so a table heading repeated at the top of each page stays
    with its table; set headers ("Heading #2") are never removed.
    """

    def masked(line: Line) -> str:
        return re.sub(r"\d+", "#", line.text)

    margin_lines = []
    for lines, height in zip(pages, heights):
        blocks: dict[int, list[Line]] = {}
        for line in lines:
            blocks.setdefault(line.block, []).append(line)
        margin_lines.append(
            [
                line
                for block in blocks.values()
                if all(line.y[1] < HEADER_FOOTER_MARGIN * height for line in block)
                or all(
                    line.y[0] > (1 - HEADER_FOOTER_MARGIN) * height for line in block
                )
                for line in block
            ]
        )

    page_counts: dict[str, int] = {}
    for lines in margin_lines:
        for text in {masked(line) for line in lines}:
            page_counts[text] = page_counts.get(text, 0) + 1
    repeated = {
        text for text, count in page_counts.items() if count >= HEADER_FOOTER_MIN_PAGES
    }
    for lines, margin in zip(pages, margin_lines):
        to_remove = {
            id(line)
            for line in margin
            if masked(line) in repeated and not SET_HEADER_RE.match(line.text)
        }
        lines[:] = [line for line in lines if id(line) not in to_remove]


def _is_item_row(line: Line) -> bool:
    return bool(
        QUANTITY_START_RE.match(line.text) and HARDWARE_WORD_RE.search(line.text)
    )


def set_header_lines(lines: list[Line]) -> list[Line]:
    """The lines on a page that start a set."""
    return [line for line in lines if SET_HEADER_RE.match(line.text)]


def _continues_set(lines: list[Line]) -> bool:
    """True if the page holds something before its first set header: the rest of a
    set from the previous page (more rows, a repeated table heading, its notes)."""
    return bool(lines) and not SET_HEADER_RE.match(lines[0].text)


def find_hardware_pages(pages: list[list[Line]]) -> list[int]:
    """The page numbers to send to the model.

    Valor Acres (18 pages) -> [4, 6, 7, ..., 18]; nothing else in the book is sent.
    """
    flagged = {
        page_number
        for page_number, lines in enumerate(pages, start=1)
        if _is_hardware_page(lines)
    }
    # Fill one-page holes, e.g. a page of notes in the middle of the schedule.
    flagged |= {
        page_number
        for page_number in range(2, len(pages))
        if {page_number - 1, page_number + 1} <= flagged
    }
    # Add a page either side of each run, in case a set's header page or last rows
    # have too few item rows to be flagged. Single pages are usually stray matches.
    padded = set(flagged)
    for run in _consecutive_runs(sorted(flagged)):
        if len(run) > 1:
            padded |= {run[0] - 1, run[-1] + 1}
    # Add the book's code tables (manufacturer, finish and option lists), which often
    # sit a page or two before the first set.
    padded |= {
        page_number
        for page_number, lines in enumerate(pages, start=1)
        if any(CODE_TABLE_RE.match(line.text) for line in lines)
        and any(abs(page_number - other) <= CODE_TABLE_REACH for other in padded)
    }
    return sorted(
        page_number for page_number in padded if 1 <= page_number <= len(pages)
    )


def _is_hardware_page(lines: list[Line]) -> bool:
    item_rows = sum(_is_item_row(line) for line in lines)
    if item_rows >= MIN_ITEM_ROWS:
        return True
    mentions = [line for line in lines if SET_MENTION_RE.search(line.text)]
    return bool(mentions) and (
        item_rows > 0 or any(NOT_USED_RE.search(line.text) for line in mentions)
    )


def _consecutive_runs(page_numbers: list[int]) -> list[list[int]]:
    """[3, 4, 5, 9, 10] -> [[3, 4, 5], [9, 10]]"""
    runs: list[list[int]] = []
    for page_number in page_numbers:
        if runs and runs[-1][-1] == page_number - 1:
            runs[-1].append(page_number)
        else:
            runs.append([page_number])
    return runs


def group_pages(pages: list[list[Line]], page_numbers: list[int]) -> list[list[int]]:
    """Group pages into model requests, avoiding splitting a set between two requests.

    Pages are first chained into segments that must stay together (a page that
    continues a set joins the page before). Whole segments are then packed into
    groups of about PAGES_PER_REQUEST pages, at most MAX_PAGES_PER_REQUEST; only a
    segment longer than that is cut, and the model's continuation flag lets the
    pieces be joined again afterwards.
    """
    segments: list[list[int]] = []
    for page_number in page_numbers:
        if (
            segments
            and segments[-1][-1] == page_number - 1
            and _continues_set(pages[page_number - 1])
        ):
            segments[-1].append(page_number)
        else:
            segments.append([page_number])

    groups: list[list[int]] = []
    for segment in segments:
        group = groups[-1] if groups else None
        if (
            group
            and group[-1] == segment[0] - 1
            and len(group) < PAGES_PER_REQUEST
            and len(group) + len(segment) <= MAX_PAGES_PER_REQUEST
        ):
            group.extend(segment)
        else:
            groups.append(list(segment))
    return [
        group[start : start + MAX_PAGES_PER_REQUEST]
        for group in groups
        for start in range(0, len(group), MAX_PAGES_PER_REQUEST)
    ]


def render_page(lines: list[Line]) -> str:
    """The page as the model reads it: rendered lines, a blank line between blocks."""
    rendered: list[str] = []
    for previous, line in itertools.pairwise([None, *lines]):
        if previous is not None and line.block != previous.block:
            rendered.append("")
        rendered.append(line.render())
    return "\n".join(rendered)
