# Fresco Hardware Set API

Extracts door hardware sets from Division 08 specification PDFs: each set's number,
title, the doors it's used on, its components (qty, description, catalog number,
manufacturer, finish, notes), set notes, operational description, and where it sits
in the PDF (page and bounding box).

- **Web app:** https://fresco-web-rfhi.onrender.com (upload a PDF, check each page's set
  boxes, correct the JSON)
- **API:** https://fresco-zxc7.onrender.com
- **Docs:** https://fresco-zxc7.onrender.com/docs (interactive) and `/redoc`
- **Loom walkthrough:** https://www.loom.com/share/c2527e289de542b58993e32b690d56e9

```bash
curl -F file=@spec.pdf https://fresco-zxc7.onrender.com/extract
```

## Pipeline

1. **Read.** Every page becomes numbered lines (`p7.15`) of cells, split at wide
   horizontal gaps so table columns stay apart. Sideways margin text, icon glyphs,
   and repeated page headers and footers are dropped; extra vertical whitespace marks
   blocks (set header, table, notes).
2. **Pick pages.** Only pages that look like hardware listings are kept (rows that
   start with a quantity and name a hinge, closer, ...), plus the book's code tables
   (manufacturer, finish, and option lists) near them, so a 700-page manual becomes
   the 10–40 pages that matter, with no model calls.
3. **Group pages.** Pages go to the model about 3 at a time, cut only where a new set
   starts (up to 5 pages), so a set running onto the next page is read in one
   request.
4. **Ask the model.** Claude Sonnet 5.5 reads each group as `p7.15 | [84] 3 EA | [136]
   BB HINGE | [263] BLK`, where `[x]` is a cell's position, so it can tell columns
   apart; that is how manufacturer and finish codes ("PE", "NO", ...) are separated.
   The prompt holds the rules and hand-checked worked examples, and every value
   cites the lines it came from. The model also copies out any code tables it sees.
   Requests run in parallel; the first goes alone so the rest read the prompt from
   Anthropic's prompt cache.
5. **Assemble and check.** Sets split between requests are joined, each set's
   location is the box around its cited lines, every field is checked against those
   lines, and codes are resolved: manufacturers first with the book's own code table
   (`HA` → Hager Companies), then with `codes.json` (`IVE` → Ives, `US26D` → 626).
   The book's tables, including finish and option codes (`EC2` → Flush Endcap), are
   returned in `code_tables` with their location. A warning is added if the number of
   sets differs from the set headers on the pages.

Answers are cached in Postgres per request, so the same pages are never paid for
twice.

## Output

```jsonc
{
  "filename": "087100-DOOR-HARDWARE.pdf",
  "page_count": 18,
  "hardware_pages": [4, 6, 7, ...],
  "sets": [{
    "set_number": "01",
    "description": "Door U1 – Interior Unit Entry Swing Doors",
    "not_used": false,
    "openings": ["U1"],
    "location": [{"page": 7, "x": [72.0, 403.0], "y": [261.6, 428.4]}],
    "components": [{
      "qty": 3, "unit": "EA", "description": "BB HINGE", "catalog_number": null,
      "mfr": null, "finish": "BLK", "notes": null,
      "mfr_normalized": null, "finish_normalized": null,
      "confidence": {"qty": "found", "description": "found", "finish": "found"},
      "warnings": []
    }],
    "notes": [],
    "operation": null,
    "warnings": []
  }],
  "code_tables": [],  // the book's own codes: {"kind": "option", "code": "EC2",
                     //   "meaning": "Flush Endcap", "location": {...}}
  "unresolved_codes": {"mfr": [], "finish": ["BLK"]},
  "input_tokens": 20605, "output_tokens": 18546,
  "cache_read_tokens": 29982, "cache_write_tokens": 9994,
  "warnings": [],
  "pdf_hash": "fce3b0d7…", "corrected": false
}
```

Locations are boxes in PDF points from the page's top-left, `x: [left, right]` and
`y: [top, bottom]`; a set crossing a page break has one location per page. Each
field's `confidence` is:

- `found`: the value is in the lines it cites and passed the checks (not proof it is
  in the right set or field)
- `ambiguous`: an ambiguous code (`PE`, `NO`) placed by its column, unless the
  book's code table defines it
- `suspect`: failed a check (not in the cited lines, qty not at the start of its
  line, a finish code in the manufacturer field or the reverse); see `warnings`

Codes that neither the book's tables nor `codes.json` explain aren't penalized;
they're listed in `unresolved_codes`.

## Corrections

The API stores every extracted PDF by its SHA-256 (`pdf_hash`), with just the pages
sent to the model. The web app shows each of those pages with its set boxes next to
the JSON; edit the JSON and save, and the correction is stored with the list of
fields that changed. From then on that PDF returns the corrected result, without
calling the model.

## Endpoints

| Endpoint | |
|---|---|
| `POST /extract` | Extract a PDF (or return its stored correction); `?batch=true` for batch mode |
| `GET /documents/{pdf_hash}` | The stored result, corrected if there is a correction |
| `PUT /documents/{pdf_hash}/corrections` | Store a corrected result |
| `GET /documents/{pdf_hash}/pages/{page}.png` | A page with its set boxes drawn on |

## Running it

Requires [uv](https://docs.astral.sh/uv/), an Anthropic API key, and Postgres.

```bash
uv sync
export ANTHROPIC_API_KEY=sk-ant-...
docker run -d --name fresco-db -e POSTGRES_USER=fresco -e POSTGRES_PASSWORD=fresco \
  -e POSTGRES_DB=fresco -p 5432:5432 postgres:17
export DATABASE_URL=postgresql://fresco:fresco@localhost:5432/fresco

uv run fresco extract spec.pdf -o out.json
uv run fresco extract spec.pdf --batch -o out.json   # half price, waits for the batch
uv run fresco extract spec.pdf --dry-run             # list the pages it would send

uv run uvicorn fresco.main:app --reload             # docs at http://localhost:8000/docs
curl -F file=@spec.pdf http://localhost:8000/extract
curl -F file=@spec.pdf "http://localhost:8000/extract?batch=true"   # 202 until done

cd web && pnpm install && pnpm dev                  # web app at http://localhost:5173
pnpm openapi                                        # regenerate API types from /openapi.json
```

The web app (React, TypeScript, Vite; Node 24 and pnpm) calls the API through types
generated from its OpenAPI schema with `openapi-typescript`; `VITE_API_URL` sets the
API address.

**Live or batch.** Live answers in about a minute. Batch sends the same requests
through Anthropic's Batch API at half price; results usually take up to an hour (24
hours at most). The API answers 202 until the batch has finished, so POST the same
PDF again to get the result.

Settings: `FRESCO_MODEL` (default `claude-sonnet-5-5`), `FRESCO_EFFORT` (default
`low`). `/extract` is limited to 1 request per minute per IP.

## Cost, speed, and accuracy

One live run per model on each book, scored against Opus 5.5 (sets and components it
also found, and how often manufacturer and finish match). Sonnet is the current
pipeline; Opus and Haiku were measured on an earlier version, before set notes,
operation, and openings were added.

| Book | Model | Cost | Time | Sets | Components | Mfr / finish |
|---|---|---|---|---|---|---|
| Valor Acres (18 pages, lists) | Opus 5.5 (reference) | $0.51 | 41s | — | — | — |
| | **Sonnet 5.5 (default)** | **$0.26** | **48s** | **100%** | **100%** | **100% / 100%** |
| | Haiku 4.5 | $0.11 | 31s | 100% | 95% | 100% / 100% |
| Vantage TX-22 (470 pages, tables) | Opus 5.5 (reference) | $1.03 | 53s | — | — | — |
| | **Sonnet 5.5 (default)** | **$0.55** | **35s** | **100%** | **100%** | **100% / 100%** |
| | Haiku 4.5 | $0.24 | 36s | 100% | 98% | 100% / 100% |

Sonnet matched Opus at half the price. Haiku is about 4.5x cheaper than Opus and a
little faster, but its misses are real errors that the checks can't catch, because
every value it returns is real text, just in the wrong place: it dropped the last
components of three Valor sets, and on Vantage it put set 341's items under set 401,
whose header sits at the bottom of the page before. Agreement with Opus isn't proof
of correctness, but spot checks against the PDFs backed Opus where they differed.
Most of the cost is the model's output, so leaving empty fields out of the answer
and batch mode are what lower it.

## Tradeoffs

- **Model instead of rules for reading sets.** The layouts vary too much for rules:
  set headers can sit at the bottom of the page before their rows, a column can hold
  either a manufacturer or a finish (`PE`), and notes and operation text are mixed
  in with the rows. Code still does everything that doesn't need judgment: reading
  the PDF, choosing pages, grouping them, locations, checks, and code mapping.
- **Rules for choosing pages.** Picking pages without a model is free and fast, and
  cuts a 470-page book down to about 30 pages. The cost is that a hardware page with
  few item rows and no set header can be missed.
- **Line citations instead of model coordinates.** The model cites line ids, and the
  code computes the boxes from them, so locations are exact. Every value can also be
  checked against its cited lines. The citations add output tokens.
- **About 3 pages per request.** More pages per request would mean fewer repeated
  prompts, but longer answers and less parallelism. Cutting only where a set starts
  keeps most sets in one request. Sets that still cross a cut are joined afterwards.
- **Sonnet over Opus and Haiku.** Sonnet matched Opus at half the price. Haiku is
  cheaper again, but it makes placement errors that the checks can't catch (see
  above).
- **Checks catch made-up values, not misplaced ones.** A `found` value is real text
  from the cited lines, but it could still be in the wrong set or field.
- **Live or batch.** Live answers in about a minute. Batch costs half as much but can
  take up to an hour.

## Limitations

- **No OCR.** Scanned pages (no text layer) are skipped. OCR (`ocrmypdf`) was tried
  and left out: the scanned pages in the sample books are contract forms, reports,
  and product sheets, not schedules, and OCR costs about 5 CPU-seconds a page.
- **Heuristic page selection.** A stray page only costs a request, but a hardware
  page with few item rows and no set header can be missed.
- **Sets on drawings.** Some books put the sets on the drawings instead of in the
  spec.
- **No authentication on corrections.** Anyone with a document's `pdf_hash` can
  replace its correction. Fine for a demo; a real deployment needs accounts.
- **Letter-code items.** Code tables are resolved across the whole book, but none of
  the sample books list set items only by a letter code pointing into a product
  table, so that isn't handled.
