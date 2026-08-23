import json, os
from google import genai
from google.genai import types
from customs.config import settings

_client = None

def client() -> genai.Client:
    global _client
    if _client is None:
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
        _client = genai.Client(vertexai=True, project=settings.gcp_project,
                               location="global")
    return _client

def _generate(model, contents, config):
    return client().models.generate_content(model=model, contents=contents, config=config)

def generate_json(model: str, parts: list, schema: dict) -> dict:
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema)
    r = _generate(model, parts, cfg)
    try:
        return json.loads(r.text)
    except json.JSONDecodeError:
        r2 = _generate(model, parts, cfg)
        return json.loads(r2.text)

def generate_grounded(model: str, prompt: str) -> tuple[str, list[dict]]:
    cfg = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
    r = _generate(model, [prompt], cfg)
    chunks = []
    for cand in (r.candidates or []):
        gm = getattr(cand, "grounding_metadata", None)
        for ch in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(ch, "web", None)
            if web and web.uri:
                chunks.append({"uri": web.uri, "title": web.title or ""})
    return r.text or "", chunks
