import json, os, re
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

def generate_json(model: str, parts: list, schema: dict,
                  thinking_budget: int | None = None) -> dict:
    # temperature=0: a clearance verdict has to be reproducible. At the
    # default sampling temperature the judge gave two near-identical modesty
    # observations opposite verdicts in one run (shot_5 triggered ID-MOD-01,
    # shot_6 did not), which reads as the system being arbitrary rather than
    # strict. Greedy decoding does not make a subjective rule objective, but
    # it does mean the same film gets the same answer twice.
    #
    # thinking_budget=0 for the calls that are classification rather than
    # deliberation: filtering eighty one-line captions took 28 seconds of
    # thinking to answer with twenty numbers. Left as None, the model keeps
    # whatever budget it defaults to, which is what every judgement in this
    # system still gets.
    extra = {}
    if thinking_budget is not None:
        extra["thinking_config"] = types.ThinkingConfig(
            thinking_budget=thinking_budget)
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema,
        temperature=0.0, **extra)
    r = _generate(model, parts, cfg)
    try:
        return json.loads(r.text)
    except json.JSONDecodeError:
        r2 = _generate(model, parts, cfg)
        return json.loads(r2.text)

def generate_json_image(prompt: str, image_bytes: bytes, schema: dict,
                        mime_type: str = "image/png") -> dict:
    """A structured verdict about one image. Used to check an edited frame
    before anything expensive is spent on it."""
    parts = [prompt, types.Part.from_bytes(data=image_bytes, mime_type=mime_type)]
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema,
        temperature=0.0)
    r = _generate(settings.model_text, parts, cfg)
    return json.loads(r.text)

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


class VeoBlocked(RuntimeError):
    """Veo's safety filter rejected the generated video.

    Distinct from every other failure because Google says explicitly: "You
    will not be charged for blocked videos." Charging the operator's budget
    for one would be taking money for work nobody did.
    """


class VeoRefusedInput(RuntimeError):
    """Veo's input filter rejected the anchor frames before generating.

    A different gate from VeoBlocked: this one fires on the operation's
    error, on the way IN ("the input image violates Vertex AI's usage
    guidelines"), so zero seconds of video were produced. Veo bills per
    second of output, which is why this must not be charged -- and why it
    is worth its own class: the fix is different frames, not a re-roll.
    Gemini's image editor and Veo do not share a safety policy; a frame
    the editor happily produced and our anchor check passed can still be
    refused here.

    `categories` names WHY, decoded from the refusal's support codes
    (Google's documented table, docs.cloud.google.com responsible-ai-imagen).
    The distinction is load-bearing: a "sexual"/"people" refusal can be
    answered by re-editing the frames more conservatively, a "celebrity"
    or "child" refusal cannot -- no amount of clothing fixes a face, so
    the caller must not waste edits trying.
    """

    def __init__(self, message: str, categories: tuple[str, ...] = ()):
        super().__init__(message)
        self.categories = categories


# Google's documented support-code table for the image/video safety
# filters (verified against the responsible-ai-imagen page, 2026-08-31).
# An opaque "Support codes: 15236754" answered a real operator question --
# "did Veo refuse the woman or the perfume?" -- only after a docs dig; the
# feed should name the category itself.
_SUPPORT_CODES = {
    "58061214": "child", "17301594": "child",
    "29310472": "celebrity", "15236754": "celebrity",
    "64151117": "celebrity or child",
    "62263041": "dangerous content",
    "57734940": "hate", "22137204": "hate",
    "39322892": "people/face",
    "92201652": "personal information",
    "89371032": "prohibited content", "49114662": "prohibited content",
    "72817394": "prohibited content",
    "90789179": "sexual", "63429089": "sexual", "43188360": "sexual",
    "35561574": "third-party content", "35561575": "third-party content",
    "78610348": "toxic",
    "61493863": "violence", "56562880": "violence",
    "32635315": "vulgar",
}


def _refusal_categories(message: str) -> tuple[str, ...]:
    """The human names behind 'Support codes: NNN, NNN', deduplicated."""
    found = re.search(r"[Ss]upport codes?:\s*([\d,\s]+)", message)
    if not found:
        return ()
    names = []
    for code in re.findall(r"\d+", found.group(1)):
        name = _SUPPORT_CODES.get(code)
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _seed_from(*paths) -> int:
    """A stable seed for these exact anchor frames.

    Same anchors, same footage. Different anchors, different footage. It
    is derived rather than fixed so two unrelated bridges do not inherit
    each other's motion, and it is stable rather than random so a bridge
    can be reproduced instead of only re-rolled at 1.88-3.68 EUR a go.
    """
    import hashlib
    from pathlib import Path as _P
    digest = hashlib.sha256()
    for path in paths:
        digest.update(_P(path).read_bytes())
    # Vertex takes a uint32.
    return int.from_bytes(digest.digest()[:4], "big")


class OmniRefusedInput(RuntimeError):
    """Omni's input filter refused the footage before any work.

    Seen live 2026-09-01: a reel of famous cartoons answered 400
    prohibited_content, "the input could not be submitted due to interests
    of third-party content providers". Zero seconds ran, so nothing is
    charged, and no retry can help -- the footage itself is the refusal.
    """


class OmniQuota(RuntimeError):
    """Omni's quota on this project refused the request before any work.

    gemini-omni-1.1-flash-preview ships with a zero default quota on
    global_generate_content_requests_per_minute_per_project_per_base_model
    (probed live 2026-09-01: two spaced attempts, both 429) -- the same
    story as the image model. Nothing runs, so nothing is charged, and the
    fix is a quota increase request in the console, not a retry.
    """


def generate_omni_edit(instruction: str, clip_path, out_path,
                       poll_s: float = 10.0, timeout_s: float = 600.0):
    """Gemini Omni, video-to-video: edit this clip as instructed.

    The clip goes in whole, inline, and comes back the same length with
    only the named change made -- no anchors, no interpolation, the
    footage stays the brand's own. Input must be 10 seconds or less
    (Google's documented cap for editing uploads).

    The response contract is from the SDK's own generated types
    (VideoContent: data/uri, Interaction: status/outputs); the first
    quota-approved call is the live verification.
    """
    import base64 as _b64
    import time as _time
    from pathlib import Path as _P

    clip = _P(clip_path)

    def _create():
        return client().interactions.create(
            model=settings.model_omni,
            input=[
                {"type": "video", "mime_type": "video/mp4",
                 "data": _b64.b64encode(clip.read_bytes()).decode()},
                {"type": "text", "text": instruction},
            ])

    global _client
    try:
        try:
            interaction = _create()
        except Exception as stale:  # noqa: BLE001 -- one narrow retry
            message = str(stale)
            if "ACCESS_TOKEN_EXPIRED" not in message and \
                    not message.startswith("Error code: 401"):
                raise
            # The SDK's Interactions layer is generated code that snapshots
            # its bearer token at client construction and never refreshes
            # it -- so on a long-lived container the first omni call after
            # the first hour died 401 while every classic API kept working
            # (seen live 2026-09-01, ACCESS_TOKEN_EXPIRED on
            # CreateInteractionHttp). A fresh client mints a fresh token.
            _client = None
            interaction = _create()
    except Exception as exc:  # noqa: BLE001 -- classify, then re-raise
        message = str(exc)
        if "429" in message or "Quota exceeded" in message:
            raise OmniQuota(
                "Omni answered quota-exceeded before any work: nothing ran "
                "and nothing was charged. On gemini-omni-1.1-flash-preview "
                "this is an access gate wearing a quota error (the granted "
                "quota reads 10 and request one still 429s); the configured "
                "alias should not hit it.") from exc
        if "prohibited_content" in message or "third-party content" in message:
            # Omni checks its INPUT -- a famous cartoon reel and the Chanel
            # spot were both refused over "interests of third-party content
            # providers". The code covers more reasons than that one, so the
            # feed quotes Omni's own words instead of asserting a cause.
            import re as _re
            said = _re.search(r"'message':\s*'([^']+)'", message)
            raise OmniRefusedInput(
                "Omni's content filter refused the input footage itself. "
                "Nothing was charged, and a retry cannot help: the footage "
                "is the refusal. Use a patch method instead. Omni said: "
                + (said.group(1) if said else message[:200])) from exc
        raise

    waited = 0.0
    while getattr(interaction, "status", "") in ("queued", "pending",
                                                 "in_progress", "running"):
        _time.sleep(poll_s)
        waited += poll_s
        if waited > timeout_s:
            raise RuntimeError(f"Omni edit still running after {waited:.0f}s")
        interaction = client().interactions.get(getattr(interaction, "id"))

    status = getattr(interaction, "status", "")
    if status not in ("completed", ""):
        raise RuntimeError(f"Omni edit ended {status}: "
                           f"{str(interaction)[:300]}")

    # The easy path first: the interaction carries a convenience
    # `output_video` field (verified live 2026-09-01 -- outputs[] holds a
    # "thought" block and a "model_output" whose content is the video, and
    # this field is the same bytes without the walk).
    direct = getattr(interaction, "output_video", None)
    data = getattr(direct, "data", None) if direct is not None else None
    if data:
        out = _P(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_b64.b64decode(data) if isinstance(data, str) else data)
        return out

    def _blocks(node):
        for item in (getattr(node, "outputs", None) or []):
            yield item
            for sub_item in (getattr(item, "content", None) or []):
                yield sub_item

    for block in _blocks(interaction):
        if getattr(block, "type", "") != "video":
            continue
        data = getattr(block, "data", None)
        if data:
            out = _P(out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(_b64.b64decode(data) if isinstance(data, str) else data)
            return out
        uri = getattr(block, "uri", None)
        if uri:
            raise RuntimeError(
                f"Omni returned the video as a uri ({uri[:80]}), which this "
                f"client does not fetch yet -- teach generate_omni_edit.")
    raise RuntimeError(f"Omni returned no video block: {str(interaction)[:300]}")


def generate_bridge(prompt: str, first_frame, last_frame, seconds: float,
                    out_path, poll_s: float = 10.0, timeout_s: float = 600.0,
                    seed: int | None = None):
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
        # Reproducible. Without a seed, pressing the button twice on the
        # same finding buys two different pieces of footage at 1.88-3.68
        # EUR each, so an editor with a near miss cannot iterate -- only
        # re-roll. Derived from the anchors so the same span regenerates
        # the same way, and a genuine retry can pass a different one.
        seed=seed if seed is not None else _seed_from(first, last),
        # NOT enhance_prompt=False. It was set here for a real reason --
        # Vertex otherwise rewrites the prompt with its own model, and this
        # prompt is the one input the bridge guards hardest, deliberately
        # task-free so nothing about the violation can leak into the
        # picture. Veo simply does not allow it: the API answers "Veo 3
        # prompt enhancement cannot be disabled", and it answers it AFTER
        # both anchors have been edited and checked, which is the most
        # expensive possible place to find out.
        # Commercials are made of people. Left unset this defaults to a
        # stricter policy and rejects perfectly ordinary advertising
        # footage; the anchors are the brand's own frames of adults.
        person_generation="allow_adult",
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

    # The error first. An operation that fails carries `error` and NOTHING
    # in response or result, so reading only those reported the failure as
    # "Veo returned no video: None" and threw the actual reason away --
    # which is how an unsupported parameter looked like an empty answer.
    failed = getattr(operation, "error", None)
    if failed:
        message = (failed.get("message") if isinstance(failed, dict)
                   else getattr(failed, "message", None)) or str(failed)
        lowered = message.lower()
        if "input image" in lowered and ("violat" in lowered or "guidelines" in lowered):
            categories = _refusal_categories(message)
            named = f" [{', '.join(categories)}]" if categories else ""
            raise VeoRefusedInput(
                f"Veo refused the anchor frames{named}: {message}",
                categories=categories)
        raise RuntimeError(f"Veo refused the request: {message}")

    result = getattr(operation, "response", None) or getattr(operation, "result", None)
    videos = getattr(result, "generated_videos", None) or []
    if not videos:
        blocked = getattr(result, "rai_media_filtered_count", 0) or 0
        if blocked:
            reasons = getattr(result, "rai_media_filtered_reasons", None) or []
            raise VeoBlocked(
                "Veo's safety filter rejected the generated video. Google "
                "does not charge for a blocked generation, so neither do we. "
                + (str(reasons[0])[:200] if reasons else ""))
        raise RuntimeError(f"Veo returned no video: {result!r}")
    data = videos[0].video.video_bytes
    if not data:
        client().files.download(file=videos[0].video)
        data = videos[0].video.video_bytes
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out
