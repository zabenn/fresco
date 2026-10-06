"""The model: its prompt (rules and worked examples), the answer format, and requests,
live or through Anthropic's Batch API, with answers cached in the database."""

import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Literal, Self

import anthropic
from anthropic.lib._parse._transform import transform_schema
from pydantic import BaseModel, TypeAdapter, ValidationError

from fresco.database import Database

MODEL = os.environ.get("FRESCO_MODEL", "claude-sonnet-5-5")
EFFORT = os.environ.get("FRESCO_EFFORT", "low")
MAX_TOKENS = 32000  # a dense 5-page request can pass 16K

RULES = """\
You extract door hardware sets from construction specification pages (Division 08, \
usually section 08 71 00). A hardware set (also called a group or heading) is a \
numbered group of components assigned to doors, e.g. "SET #1 - ENTRANCE DOORS", \
"Heading #3", "HARDWARE GROUP NO. D725Z", "Hardware Group/Set #A1". Some books lay \
sets out as indented lists, others as tables with columns such as QTY, DESCRIPTION, \
CATALOG NUMBER, FINISH, MFR.

Input format: one line per visual line of the page. Each line is
    <line_id> | [x] cell | [x] cell ...
where [x] is the cell's left edge in PDF points. Cells that share an x-position across \
rows are the same column. A blank line marks extra whitespace on the page: a table, a \
set header and a notes paragraph usually sit in separate blocks, while text wrapped \
inside a table cell stays in the table's block. Page headers and footers are removed.

Rules:
- Extract every hardware set on these pages, in page order, including sets marked \
NOT USED / N/A (set not_used=true, components empty).
- Ignore everything that is not a set: general spec text, product requirements, \
door schedules/indexes listing door numbers, page headers and footers.
- set_number is the identifier exactly as printed without the label: "1", "3A", \
"EX-1.0", "D725Z", "001". description is the set's title if one is printed (e.g. \
"ENTRANCE DOORS"). Do not use door lists or operational notes as the description.
- openings: the door/opening numbers the set is used on, as printed, e.g. "D168" from \
"Doors: D168", "C717" from "Item #435 1 Single door C717, PARTY ROOM …", "201" from \
"1 Single Door #201". These door lines are not components.
- If component lines or notes at the top of these pages continue a set whose header is \
not on these pages, emit that set first with continues_previous_set=true and set_number \
null (unless the page repeats the number, e.g. "Set 4 (continued)").
- One component per hardware item. qty is the number printed (null if none printed, \
never guess); unit is EA/PR/SET etc. if printed. description is the item name (e.g. \
"Hinge", "Exit Device"). catalog_number is the model/catalog designation with its \
options. mfr is the manufacturer abbreviation or name, finish is the finish code, \
exactly as printed. notes holds anything else on that item (e.g. "by door supplier", \
"existing to remain"). Items listed without catalog data (e.g. "Door position switch \
by Div 28") are still components.
- Set notes: a note printed for the set as a whole, under or after its items (e.g. \
"NOTE: FURNISH ADDITIONAL HINGES AS REQUIRED FOR DOORS 7'6" AND OVER"), even when it \
runs onto the next page, goes in the set's notes: one string per note, wrapped lines \
joined, without the "NOTE:" label. Cite its lines in note_line_ids. A note about one \
item stays in that component's notes.
- Operation: an operational description printed for the set ("Operational \
Description:", "OPERATION:", "Notes: Operation:") says how the door works: locking, \
egress, power loss, what each button does. Put its full text in operation (lines \
joined, label dropped) and cite its lines in operation_line_ids. It is not a set note.
- Manufacturer vs finish: short codes are ambiguous ("PE" = Pemko or painted enamel, \
"NO" = Norton or "No."). Decide from the column, not the value: a column mostly \
holding MK, LCN, SCH, IVE, VON, ZER, PEM is the manufacturer column; one holding 626, \
630, US26D, 689, BSP, AL is the finish column. A finish code inside the catalog text \
(e.g. "4040XP 689") belongs in finish only if there is no separate finish column.
- line_ids / header_line_ids / note_line_ids / operation_line_ids: cite every line each \
value was read from (header_line_ids covers the set header, its title and its opening \
lines). Never invent text; every value must appear in the cited lines.
- Code tables: if these pages print the book's own code table, such as a manufacturer \
list or legend ("HA  Hager Companies"), a finish list ("S  Charcoal") or an option list \
("EC2  Flush Endcap", "LAR  Length as Required"), put every entry in codes: kind \
"mfr", "finish" or "option" (catalog options and abbreviations used in catalog \
numbers), the code and its meaning exactly as printed, and the line it is on. A table \
continued from a previous page is still a code table. Codes are not components.
- Leave out optional fields that would be empty or false; use null for a qty or \
set_number that isn't printed. A placeholder such as "---" or "--" in a column means \
no value.
- If these pages contain no hardware sets, return "sets": [] (with any code table \
entries still in codes)."""


# Optional fields have defaults so the model can leave them out when empty, which
# makes answers ~14% shorter (output is most of the cost). Structured outputs reject
# a schema with too many optional or nullable fields, so optional fields are plain
# types that default to "" / [] / False, and only qty and set_number are nullable.
class ModelComponent(BaseModel):
    qty: float | None
    description: str
    line_ids: list[str]
    unit: str = ""
    catalog_number: str = ""
    mfr: str = ""
    finish: str = ""
    notes: str = ""


class ModelSet(BaseModel):
    set_number: str | None
    header_line_ids: list[str]
    components: list[ModelComponent]
    description: str = ""
    not_used: bool = False
    continues_previous_set: bool = False
    openings: list[str] = []
    notes: list[str] = []
    note_line_ids: list[str] = []
    operation: str = ""
    operation_line_ids: list[str] = []


class ModelCode(BaseModel):
    kind: Literal["mfr", "finish", "option"]
    code: str
    meaning: str
    line_ids: list[str]


class ModelAnswer(BaseModel):
    sets: list[ModelSet]
    codes: list[ModelCode] = []


REQUIRED_FIELDS = {
    "sets",
    "kind",
    "code",
    "meaning",
    "set_number",
    "header_line_ids",
    "components",
    "qty",
    "line_ids",
}


def _compact(value: Any) -> Any:
    """Drop empty optional fields from an answer, the way the model is told to."""
    if isinstance(value, dict):
        return {
            key: _compact(item)
            for key, item in value.items()
            if key in REQUIRED_FIELDS or item not in (None, [], "", False)
        }
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _examples_text() -> str:
    """The hand-checked examples in examples/*.json as prompt text."""
    examples = []
    for path in sorted((Path(__file__).parent / "examples").glob("*.json")):
        example = json.loads(path.read_text())
        answer = json.dumps(
            _compact(example["output"]), ensure_ascii=False, separators=(",", ":")
        )
        examples.append(
            f"<example>\nInput:\n{example['input']}\n\nCorrect output:\n{answer}\n"
            "</example>"
        )
    return "Worked examples from real spec books:\n\n" + "\n\n".join(examples)


EXAMPLES = _examples_text()
# The cache marker on the last block caches the rules and examples together.
SYSTEM_BLOCKS = [
    {"type": "text", "text": RULES},
    {"type": "text", "text": EXAMPLES, "cache_control": {"type": "ephemeral"}},
]
# The schema messages.parse() sends for ModelAnswer; batch requests need it as JSON.
ANSWER_SCHEMA = transform_schema(TypeAdapter(ModelAnswer).json_schema())


class LLM:
    """The model, with answers cached in the database.

        with Database() as database:
            async with LLM(database) as llm:
                answer, tokens = await llm.extract_sets(request_text)

    Entering creates the Anthropic client; leaving closes it.
    """

    def __init__(
        self, database: Database, model: str = MODEL, effort: str = EFFORT
    ) -> None:
        self.database = database
        self.model = model
        self.effort = effort
        self._client: anthropic.AsyncAnthropic | None = None

    async def __aenter__(self) -> Self:
        self._client = anthropic.AsyncAnthropic()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def extract_sets(
        self, request_text: str, started: asyncio.Event | None = None
    ) -> tuple[ModelAnswer, dict[str, int]]:
        """The model's answer for one request, from the cache if possible.

        Returns the answer and the tokens spent ({} on a cache hit). `started` is set
        when the response starts streaming, which is when the prompt it wrote to
        Anthropic's prompt cache becomes readable by other requests. Raises
        ValueError if there is no usable answer; failures are never cached.
        """
        key = self._cache_key(request_text)
        if cached := self.database.get_answer(key):
            return ModelAnswer.model_validate(cached), {}
        response = await self._stream(request_text, started)
        answer = response.parsed_output
        self.database.put_answer(key, self.model, answer.model_dump_json())
        return answer, _token_counts(response.usage)

    async def submit_batch(self, request_texts: list[str]) -> str | None:
        """Send the requests that aren't cached yet as one batch (half price, usually
        done within the hour). Returns the batch id, or None if all are cached."""
        requests: dict[str, dict] = {}
        for text in request_texts:
            key = self._cache_key(text)
            if key not in requests and self.database.get_answer(key) is None:
                # The cache key doubles as the request id, so collect_batch knows
                # where each answer belongs.
                requests[key] = {"custom_id": key, "params": self._request_params(text)}
        if not requests:
            return None
        batch = await self._require_client().messages.batches.create(
            requests=list(requests.values())
        )
        return batch.id

    async def collect_batch(self, batch_id: str) -> dict[str, int] | None:
        """None while the batch runs; once it has ended, caches every usable answer
        and returns the tokens the batch used. Requests that failed stay uncached and
        are retried live by the next extraction."""
        client = self._require_client()
        batch = await client.messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            return None
        tokens: Counter[str] = Counter()
        async for item in await client.messages.batches.results(batch_id):
            if item.result.type != "succeeded":
                continue
            message = item.result.message
            tokens.update(_token_counts(message.usage))
            if message.stop_reason in ("refusal", "max_tokens"):
                continue
            text = next(
                (block.text for block in message.content if block.type == "text"), ""
            )
            try:
                answer = ModelAnswer.model_validate_json(text)
            except ValidationError:
                continue
            self.database.put_answer(
                item.custom_id, self.model, answer.model_dump_json()
            )
        return dict(tokens)

    def batch_key(self, request_texts: list[str]) -> str:
        """Identifies one PDF's requests: the same pages, model and prompt."""
        keys = "".join(self._cache_key(text) for text in request_texts)
        return hashlib.sha256(keys.encode()).hexdigest()

    def _cache_key(self, request_text: str) -> str:
        """Covers everything that shapes an answer, so a new model or prompt never
        reuses an old answer and a revised PDF only misses on the changed pages."""
        schema = ModelAnswer.model_json_schema()
        parts = [self.model, self.effort, RULES, EXAMPLES, schema, request_text]
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()

    async def _stream(self, request_text: str, started: asyncio.Event | None):
        options: dict[str, Any] = {}
        if not self.model.startswith("claude-haiku"):  # Haiku 4.5 takes neither
            options = {
                "output_config": {"effort": self.effort},
                "betas": ["server-side-fallback-2026-07-01"],
                "fallbacks": "default",
            }
        async with self._require_client().beta.messages.stream(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_BLOCKS,
            messages=[{"role": "user", "content": request_text}],
            output_format=ModelAnswer,
            **options,
        ) as stream:
            async for _ in stream:
                if started is not None:
                    started.set()
                    started = None
            response = await stream.get_final_message()
        if (
            response.stop_reason in ("refusal", "max_tokens")
            or not response.parsed_output
        ):
            raise ValueError(
                f"no usable model output (stop_reason={response.stop_reason})"
            )
        return response

    def _request_params(self, request_text: str) -> dict[str, Any]:
        """A batch request: the same as _stream's, except the Batch API rejects
        fallbacks and takes the answer schema as JSON."""
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}
        }
        if not self.model.startswith("claude-haiku"):
            output_config["effort"] = self.effort
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_BLOCKS,
            "messages": [{"role": "user", "content": request_text}],
            "output_config": output_config,
        }

    def _require_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            raise RuntimeError("use the LLM inside `async with`")
        return self._client


def _token_counts(usage: Any) -> dict[str, int]:
    """Input tokens exclude the prompt read from or written to the prompt cache."""
    return {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_read": usage.cache_read_input_tokens or 0,
        "cache_write": usage.cache_creation_input_tokens or 0,
    }
