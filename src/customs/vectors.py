"""A search index over the captions, so a question does not cost a reading.

The first version of semantic search sent every candidate caption to Gemini
on every query. It was right and it was slow: a hundred seconds for "a
person smoking", and a 429 on the search after it. Reading four thousand
sentences to answer one question is work that should have been done once.

So it is done once. Every caption is embedded when it is written, the
vector is kept beside the observation in SQLite, and a question is answered
by embedding the question alone and taking the nearest captions. That is
one small model call and a dot product, which is milliseconds rather than
minutes, and it is the same answer for the tenth person who asks.

Three deliberate cheapnesses, each with its ceiling named:

* 256 dimensions, not the model's full width. Recall on one-sentence
  captions is indistinguishable at this size and the arithmetic is three
  times lighter.
* Vectors are stored normalised, so similarity is a plain dot product and
  nothing has to be divided at query time.
* Pure Python for that dot product. numpy would be faster and is not
  installed; at a few thousand captions this is a tenth of a second, and
  the day this instance holds a hundred thousand is the day to add it.
  # ponytail: linear scan, an ANN index if the corpus ever outgrows a laptop

What it is NOT is the whole answer. Nearest is not the same as right: a
carrot is near a rabbit and is not one. The index narrows to the handful
worth reading, and the model still decides which of those the question is
actually about.
"""
from __future__ import annotations

import array
import logging
import math

log = logging.getLogger(__name__)

# Small on purpose; see the module docstring.
DIMS = 256
MODEL = "text-embedding-005"
# How many captions go to Vertex in one embed call.
_EMBED_BATCH = 100


def _pack(values) -> bytes:
    """A vector as bytes, normalised so similarity is a dot product."""
    vec = array.array("f", (float(v) for v in values))
    length = math.sqrt(sum(v * v for v in vec)) or 1.0
    return array.array("f", (v / length for v in vec)).tobytes()


def _unpack(blob: bytes) -> array.array:
    vec = array.array("f")
    vec.frombytes(blob)
    return vec


def embed(texts: list[str], *, query: bool = False, model: str = MODEL) -> list[bytes]:
    """Vectors for these strings, normalised and packed.

    `query=True` tells the model the text is a question rather than a
    document, which is what the task_type distinction is for and is worth
    the one keyword: asking and being asked are not the same shape.
    """
    from google.genai import types

    from customs.genai_client import client

    out: list[bytes] = []
    task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
    for i in range(0, len(texts), _EMBED_BATCH):
        chunk = texts[i:i + _EMBED_BATCH]
        answer = client().models.embed_content(
            model=model, contents=chunk,
            config=types.EmbedContentConfig(output_dimensionality=DIMS,
                                            task_type=task))
        out.extend(_pack(e.values) for e in answer.embeddings)
    return out


def index_run(store, run_id: str) -> int:
    """Embed this run's captions. Returns how many were added.

    Called after a run's observations are written, and again by the
    backfill. Already-indexed observations are skipped, so it is safe to
    call twice and safe to call on a run that half-finished.
    """
    rows = [(o.id, (o.statement or "").strip()) for o in store.observations(run_id)]
    return _index(store, run_id, [r for r in rows if r[1]])


def backfill(store, limit: int = 400) -> int:
    """Index whatever is not indexed yet, newest runs first.

    Bounded, because this runs inside a request: a search on a cold index
    should be slower once, not time out. Call it again and it continues.
    """
    added = 0
    for run in store.recent_runs(200):
        if added >= limit:
            break
        added += index_run(store, run.id)
    return added


def _index(store, run_id: str, rows: list[tuple[str, str]]) -> int:
    if not rows:
        return 0
    known = store.indexed_observations(run_id)
    fresh = [(oid, text) for oid, text in rows if oid not in known]
    if not fresh:
        return 0
    try:
        vectors = embed([text for _, text in fresh])
    except Exception as exc:  # noqa: BLE001 -- an unindexed run is slow, not broken
        log.warning("could not embed %s: %r", run_id, exc)
        return 0
    store.add_vectors((oid, run_id, text, vec)
                      for (oid, text), vec in zip(fresh, vectors))
    return len(fresh)


def nearest(store, question: str, k: int = 80, model: str = MODEL) -> list[dict]:
    """The k captions closest to the question, best first.

    A linear scan over every vector in the store. Ten thousand of them is
    still a tenth of a second, and being able to say exactly what it does
    is worth more here than being able to say it is sublinear.
    """
    wanted = _unpack(embed([question], query=True, model=model)[0])
    rows = store.all_vectors()

    scored = []
    for observation_id, run_id, statement, blob in rows:
        vec = _unpack(blob)
        if len(vec) != len(wanted):
            continue
        score = 0.0
        for a, b in zip(wanted, vec):
            score += a * b
        scored.append({"observation_id": observation_id, "run_id": run_id,
                       "statement": statement, "score": score})
    scored.sort(key=lambda r: -r["score"])
    return scored[:k]


def size(store) -> int:
    return store.vector_count()
