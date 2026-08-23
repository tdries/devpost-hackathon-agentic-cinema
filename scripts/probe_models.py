"""Live probe for real Vertex AI model ids.

Tries candidate ids for the vision/text family with a live generate_content
"ping" call and stops at the first one that answers, using the shared
genai_client.client() (Vertex, location="global").

Lists imagen/veo/tts publisher models by filtering client().models.list()
on a keyword (no generation for those families). Vertex's "global" location
only surfaces generateContent-compatible Gemini models (confirmed live: veo
and imagen never appear there for this project), so this part lists from a
concrete region instead, where Vertex's Model Garden catalog is fully
populated.

If a family has zero list matches anywhere (this project has no "imagen"
match in any region checked), falls back to direct models.get() calls on a
short list of plausible ids and reports what resolves, per the model-get
fallback the controller specified for cases where list() doesn't help.

Prints KEY=value lines at the end for a human to paste into .env; this
script never writes .env itself.

Run with:
    source .venv/bin/activate && python scripts/probe_models.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google import genai
from customs.config import settings
from customs.genai_client import _generate

TEXT_VISION_CANDIDATES = [
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview",
]

# client() (genai_client.py) is pinned to location="global" for the shared
# generateContent path. Listing Imagen/Veo/TTS needs a concrete region: on
# "global" this project sees only 23 generateContent-compatible Gemini
# models; us-central1 exposes the full ~128-model Model Garden catalog.
CATALOG_LOCATION = "us-central1"

# Last-resort candidates tried via models.get() when a keyword has zero
# matches in the catalog list.
GET_FALLBACK_CANDIDATES = {
    "imagen": [
        "imagen-4.0-generate-001",
        "imagen-4.0-fast-generate-001",
        "imagen-4.0-ultra-generate-001",
        "imagen-3.0-capability-001",
        "imagen-3.0-generate-002",
    ],
}


def probe_text_vision() -> str | None:
    print("=== vision/text candidates (generate_content ping) ===")
    for model_id in TEXT_VISION_CANDIDATES:
        try:
            r = _generate(model_id, ["ping"], None)
            snippet = (r.text or "").strip().replace("\n", " ")[:80]
            print(f"OK   {model_id}: {snippet!r}")
            return model_id
        except Exception as e:
            print(f"FAIL {model_id}: {type(e).__name__}: {e}")
    return None


_catalog = None


def _catalog_client() -> genai.Client:
    # Memoized: instantiating a fresh genai.Client() per call breaks later
    # calls with "Cannot send a request, as the client has been closed"
    # once an earlier instance is garbage-collected (shared httpx resource
    # torn down on __del__). One instance, reused for every list/get call.
    global _catalog
    if _catalog is None:
        _catalog = genai.Client(vertexai=True, project=settings.gcp_project, location=CATALOG_LOCATION)
    return _catalog


def list_models_filtered(keyword: str) -> list[str]:
    print(f"\n=== models matching '{keyword}' (list @ {CATALOG_LOCATION}, no generate) ===")
    ids = []
    try:
        for m in _catalog_client().models.list():
            name = getattr(m, "name", None) or ""
            short = name.rsplit("/", 1)[-1]
            if keyword in short.lower():
                ids.append(short)
                print(short)
    except Exception as e:
        print(f"FAIL listing models for '{keyword}': {type(e).__name__}: {e}")
    if not ids:
        print(f"(no matches for '{keyword}')")
    return ids


def get_model_fallback(keyword: str) -> str | None:
    candidates = GET_FALLBACK_CANDIDATES.get(keyword, [])
    if not candidates:
        return None
    print(f"\n=== '{keyword}' had no list matches; trying model-get on {len(candidates)} candidates ===")
    c = _catalog_client()
    for cid in candidates:
        try:
            c.models.get(model=cid)
            print(f"OK   {cid}")
            return cid
        except Exception as e:
            print(f"FAIL {cid}: {type(e).__name__}: {e}")
    print(f"(none of the {keyword} candidates resolved)")
    return None


def main() -> None:
    confirmed_text = probe_text_vision()

    imagen_ids = list_models_filtered("imagen")
    if not imagen_ids:
        fallback = get_model_fallback("imagen")
        if fallback:
            imagen_ids = [fallback]
        else:
            list_models_filtered("image")  # informational: Gemini-native image models

    veo_ids = list_models_filtered("veo")
    tts_ids = list_models_filtered("tts")

    print("\n=== paste into .env ===")
    print("# (first of N matches printed above; review the full list and adjust if needed)")
    if confirmed_text:
        print(f"GEMINI_MODEL_VISION={confirmed_text}")
        print(f"GEMINI_MODEL_TEXT={confirmed_text}")
    else:
        print("# no vision/text candidate answered; GEMINI_MODEL_VISION/TEXT unset")
    if imagen_ids:
        print(f"IMAGEN_MODEL={imagen_ids[0]}")
    else:
        print("# no imagen model found (list or model-get); consider a Gemini native")
        print("# image model from the 'image' listing above, e.g. gemini-3.1-flash-image")
    if veo_ids:
        print(f"VEO_MODEL={veo_ids[0]}")
    else:
        print("# no veo models found; VEO_MODEL unset")
    if tts_ids:
        print(f"TTS_MODEL={tts_ids[0]}")
    else:
        print("# no tts models found; TTS_MODEL unset")


if __name__ == "__main__":
    main()
