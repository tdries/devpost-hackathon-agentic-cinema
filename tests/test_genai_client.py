from customs import genai_client

class FakeResp:
    def __init__(self, text): self.text = text

def test_generate_json_repairs_once(monkeypatch):
    calls = []
    def fake_call(model, contents, config):
        calls.append(1)
        return FakeResp('not json' if len(calls) == 1 else '{"a": 1}')
    monkeypatch.setattr(genai_client, "_generate", fake_call)
    out = genai_client.generate_json("m", ["p"], {"type": "object"})
    assert out == {"a": 1} and len(calls) == 2


def test_the_bridge_never_asks_veo_to_disable_prompt_enhancement():
    """Veo 3 answers "prompt enhancement cannot be disabled" -- and it
    answers it AFTER both anchors have been edited and checked, so the
    spend is already gone when the refusal arrives.

    It was set deliberately: Vertex otherwise rewrites the bridge prompt,
    which is the one input this code guards hardest. Veo does not allow
    it, so the guarding has to happen some other way.
    """
    import inspect
    from customs import genai_client

    src = inspect.getsource(genai_client.generate_bridge)
    # the comment explaining why NOT to set it is allowed to say the words;
    # what matters is that no line actually passes it
    passed = [ln for ln in src.splitlines()
              if "enhance_prompt" in ln and not ln.strip().startswith("#")]
    assert passed == [], passed


def test_a_failed_operation_reports_why_instead_of_none():
    """An operation that fails carries `error` and nothing in response or
    result. Reading only those turned every refusal into "Veo returned no
    video: None" and threw the reason away."""
    import pytest
    from customs import genai_client

    class _Op:
        done = True
        error = {"message": "Veo 3 prompt enhancement cannot be disabled."}
        response = None
        result = None

    src = __import__("inspect").getsource(genai_client.generate_bridge)
    assert 'getattr(operation, "error", None)' in src, "the error is never read"
    assert src.index('"error"') < src.index('"response"'), "error must be read first"


def test_an_input_image_refusal_is_its_own_exception():
    """"The input image violates Vertex AI's usage guidelines" produced zero
    seconds of video, so it must not be charged like an attempted generation
    -- and the caller's fix is different frames, not a re-roll. The message
    routing lives inline in generate_bridge; this pins it."""
    from customs import genai_client

    src = __import__("inspect").getsource(genai_client.generate_bridge)
    assert "VeoRefusedInput" in src, "input refusals routed to their own class"
    assert '"input image" in lowered' in src
    assert issubclass(genai_client.VeoRefusedInput, RuntimeError)


def test_support_codes_are_decoded_to_names():
    """"Support codes: 15236754" answered a real operator question -- did
    Veo refuse the woman or the perfume? -- only after a docs dig. The
    feed names the category itself now (15236754 = celebrity: the Chanel
    spot's anchors carry Keira Knightley's likeness)."""
    from customs.genai_client import _refusal_categories

    assert _refusal_categories(
        "the input image violates Vertex AI's usage guidelines. "
        "Support codes: 15236754") == ("celebrity",)
    assert _refusal_categories("Support codes: 58061214, 90789179") == (
        "child", "sexual")
    assert _refusal_categories("no codes here") == ()
    assert _refusal_categories("Support codes: 99999999") == ()
