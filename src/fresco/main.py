"""The API and the `fresco extract` command.

POST /extract and the command run live by default, answering in about a minute. With
batch mode (?batch=true / --batch) the requests go through Anthropic's Batch API at
half price and results usually take up to an hour; the API answers 202 until the
batch is done, so POST the same PDF again to check.

The API stores each extracted PDF by its SHA-256 (pdf_hash in the result). A
corrected result can be sent back with PUT /documents/{pdf_hash}/corrections, and from
then on that PDF returns the correction, without calling the model.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import anthropic
import pymupdf
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from fresco.database import Database
from fresco.extract import ExtractionResult, extract, extract_batch
from fresco.llm import LLM
from fresco.pdf import find_hardware_pages, read_pages

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
RATE_LIMIT = "1/minute"  # per IP; every live extraction spends model tokens
BATCH_POLL_SECONDS = 60
PAGE_IMAGE_DPI = 100


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    with Database() as database:
        async with LLM(database) as llm:
            app.state.llm = llm
            yield


def get_llm(request: Request) -> LLM:
    return request.app.state.llm


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="Fresco Hardware Set API",
    version="0.1.0",
    description="Extracts door hardware sets from Division 08 specification PDFs.",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Any website may call the API (the web app is one), so browsers allow cross-origin
# requests, including the PUT with a JSON body.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)


@app.post(
    "/extract",
    summary="Extract hardware sets from a specification PDF.",
    responses={
        202: {"description": "Batch mode: still running; POST the same PDF again."}
    },
)
@limiter.limit(RATE_LIMIT)
async def extract_endpoint(
    request: Request,
    file: Annotated[UploadFile, File(description="Specification PDF.")],
    llm: Annotated[LLM, Depends(get_llm)],
    batch: Annotated[
        bool, Query(description="Half price via Anthropic's Batch API; up to an hour.")
    ] = False,
) -> ExtractionResult:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "PDF exceeds 100 MB.")
    pdf_hash = hashlib.sha256(data).hexdigest()
    stored = llm.database.get_result(pdf_hash)
    if stored and stored[1]:
        return _stored_result(pdf_hash, *stored)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured on the server.")
    try:
        if batch:
            result = await extract_batch(data, file.filename, llm)
        else:
            result = await extract(data, file.filename, llm)
    except pymupdf.FileDataError:
        raise HTTPException(422, "File is not a readable PDF.")
    except anthropic.AuthenticationError:
        raise HTTPException(503, "The server's Anthropic API key was rejected.")
    if result is None:
        return JSONResponse(
            {
                "status": "processing",
                "detail": "Batch running. POST the same PDF with ?batch=true again to "
                "get the result (usually within the hour).",
            },
            status_code=202,
        )
    result.pdf_hash = pdf_hash
    pages_pdf = await asyncio.to_thread(_select_pages, data, result.hardware_pages)
    llm.database.put_document(
        pdf_hash, file.filename, result.hardware_pages, pages_pdf,
        result.model_dump_json(),
    )  # fmt: skip
    return result


@app.get(
    "/documents/{pdf_hash}",
    summary="A stored extraction: the corrected result if there is one.",
)
async def get_document(
    pdf_hash: str, llm: Annotated[LLM, Depends(get_llm)]
) -> ExtractionResult:
    stored = llm.database.get_result(pdf_hash)
    if stored is None:
        raise HTTPException(404, "No extraction stored for this PDF.")
    return _stored_result(pdf_hash, *stored)


@app.put(
    "/documents/{pdf_hash}/corrections",
    summary="Store a corrected result; the PDF returns it from now on.",
)
async def put_correction(
    pdf_hash: str, corrected: ExtractionResult, llm: Annotated[LLM, Depends(get_llm)]
) -> ExtractionResult:
    stored = llm.database.get_result(pdf_hash)
    if stored is None:
        raise HTTPException(404, "No extraction stored for this PDF.")
    corrected.pdf_hash, corrected.corrected = pdf_hash, True
    changes = _changed_paths(
        ExtractionResult.model_validate(stored[0]).model_dump(
            mode="json", exclude={"corrected"}
        ),
        corrected.model_dump(mode="json", exclude={"corrected"}),
    )
    llm.database.put_correction(pdf_hash, corrected.model_dump_json(), changes)
    return corrected


@app.get(
    "/documents/{pdf_hash}/pages/{page}.png",
    summary="A page with the box and number of each set on it.",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
async def get_page_image(
    pdf_hash: str, page: int, llm: Annotated[LLM, Depends(get_llm)]
) -> Response:
    stored = llm.database.get_result(pdf_hash)
    pages = llm.database.get_pages(pdf_hash)
    if stored is None or pages is None or page not in pages[0]:
        raise HTTPException(404, "Page not stored for this PDF.")
    result = _stored_result(pdf_hash, *stored)
    image = await asyncio.to_thread(
        _draw_page, pages[1], pages[0].index(page), page, result
    )
    # The boxes change when a correction is saved, so the image is never cached.
    return Response(
        image, media_type="image/png", headers={"Cache-Control": "no-store"}
    )


def _stored_result(
    pdf_hash: str, result: Any, corrected: Any | None
) -> ExtractionResult:
    if corrected:
        return ExtractionResult.model_validate(corrected)
    stored = ExtractionResult.model_validate(result)
    stored.pdf_hash = pdf_hash
    return stored


def _select_pages(data: bytes, page_numbers: list[int]) -> bytes:
    """A PDF of just these pages, so a 4,000-page book stores only its schedule."""
    with pymupdf.open(stream=data, filetype="pdf") as document:
        document.select([page_number - 1 for page_number in page_numbers])
        return document.tobytes(garbage=3, deflate=True)


def _draw_page(
    pages_pdf: bytes, index: int, page_number: int, result: ExtractionResult
) -> bytes:
    """The page as a PNG with each set's box and number drawn on it."""
    with pymupdf.open(stream=pages_pdf, filetype="pdf") as document:
        page = document[index]
        for hardware_set in result.sets:
            for location in hardware_set.location:
                if location.page != page_number:
                    continue
                box = pymupdf.Rect(
                    location.x[0], location.y[0], location.x[1], location.y[1]
                ) + (-3, -3, 3, 3)
                page.draw_rect(box, color=(0.85, 0.1, 0.1), width=1.5)
                page.insert_text(
                    (box.x1 + 4, box.y0 + 10),
                    hardware_set.set_number or "?",
                    fontsize=10,
                    color=(0.85, 0.1, 0.1),
                )
        return page.get_pixmap(dpi=PAGE_IMAGE_DPI).tobytes("png")


def _changed_paths(old: Any, new: Any, path: str = "") -> list[str]:
    """Where two JSON values differ: ["sets[3].components[0].finish", ...]."""
    if isinstance(old, dict) and isinstance(new, dict):
        return [
            change
            for key in dict.fromkeys([*old, *new])
            for change in _changed_paths(
                old.get(key), new.get(key), f"{path}.{key}" if path else key
            )
        ]
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [
            change
            for index, (old_item, new_item) in enumerate(zip(old, new))
            for change in _changed_paths(old_item, new_item, f"{path}[{index}]")
        ]
    return [] if json.dumps(old) == json.dumps(new) else [path]


def cli() -> None:
    """The `fresco` command."""
    parser = argparse.ArgumentParser(prog="fresco")
    commands = parser.add_subparsers(dest="command", required=True)
    extract_parser = commands.add_parser(
        "extract", help="Extract hardware sets from a PDF."
    )
    extract_parser.add_argument("pdf", type=Path)
    extract_parser.add_argument(
        "-o", "--output", type=Path, help="Write the JSON here."
    )
    extract_parser.add_argument(
        "--batch",
        action="store_true",
        help="Use Anthropic's Batch API: half price, usually up to an hour.",
    )
    extract_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list the pages that would be sent to the model (no API calls).",
    )
    args = parser.parse_args()
    if args.command == "extract":
        _extract_command(args)


def _extract_command(args: argparse.Namespace) -> None:
    data = args.pdf.read_bytes()
    if args.dry_run:
        pages = read_pages(data)
        print(f"{len(pages)} pages; hardware pages: {find_hardware_pages(pages)}")
        return

    run = _extract_with_batch if args.batch else _extract_live
    result = asyncio.run(run(data, args.pdf.name))
    output = result.model_dump_json(indent=2)
    if args.output:
        args.output.write_text(output)
    else:
        print(output)
    print(
        f"{len(result.sets)} sets, {result.input_tokens} in "
        f"(+{result.cache_read_tokens} cached, +{result.cache_write_tokens} cache "
        f"write) / {result.output_tokens} out tokens",
        file=sys.stderr,
    )
    if result.batch_tokens:
        print(f"batch (half price): {result.batch_tokens}", file=sys.stderr)
    for warning in result.warnings:
        print("warning:", warning, file=sys.stderr)


async def _extract_live(data: bytes, filename: str) -> ExtractionResult:
    with Database() as database:
        async with LLM(database) as llm:
            return await extract(data, filename, llm)


async def _extract_with_batch(data: bytes, filename: str) -> ExtractionResult:
    with Database() as database:
        async with LLM(database) as llm:
            while (result := await extract_batch(data, filename, llm)) is None:
                print(f"batch running; checking again in {BATCH_POLL_SECONDS}s",
                      file=sys.stderr)  # fmt: skip
                await asyncio.sleep(BATCH_POLL_SECONDS)
            return result
