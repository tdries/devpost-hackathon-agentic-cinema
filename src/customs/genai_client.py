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
    # temperature=0: a clearance verdict has to be reproducible. At the
    # default sampling temperature the judge gave two near-identical modesty
    # observations opposite verdicts in one run (shot_5 triggered ID-MOD-01,
    # shot_6 did not), which reads as the system being arbitrary rather than
    # strict. Greedy decoding does not make a subjective rule objective, but
    # it does mean the same film gets the same answer twice.
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema,
        temperature=0.0)
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


def generate_bridge(prompt: str, first_frame, last_frame, seconds: float,
                    out_path, poll_s: float = 10.0, timeout_s: float = 600.0):
    """Veo, anchored on two frames: generate the motion between them.

    The frames are the brand's own footage with the offending object already
    edited out, so the generated span starts and ends on pixels that match
    the untouched material either side of it. Veo bills per second of output
    and refuses to emit fewer than four, which is why costs.bridge_seconds
    exists and why the console prices this before anyone runs it.
    """
    from pathlib import Path
    import time as _time

    first, last = Path(first_frame), Path(last_frame)
    config = types.GenerateVideosConfig(
        duration_seconds=int(seconds),
        aspect_ratio="16:9",
        number_of_videos=1,
        resolution="720p",
        generate_audio=False,
        last_frame=types.Image(image_bytes=last.read_bytes(), mime_type="image/png"),
    )
    source = types.GenerateVideosSource(
        prompt=prompt,
        image=types.Image(image_bytes=first.read_bytes(), mime_type="image/png"),
    )
    operation = client().models.generate_videos(
        model=settings.model_video, source=source, config=config)

    waited = 0.0
    while not operation.done:
        _time.sleep(poll_s)
        waited += poll_s
        if waited > timeout_s:
            raise RuntimeError(f"Veo bridge still running after {waited:.0f}s")
        operation = client().operations.get(operation)

    result = getattr(operation, "response", None) or getattr(operation, "result", None)
    videos = getattr(result, "generated_videos", None) or []
    if not videos:
        raise RuntimeError(f"Veo returned no video: {result!r}")
    data = videos[0].video.video_bytes
    if not data:
        client().files.download(file=videos[0].video)
        data = videos[0].video.video_bytes
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out
