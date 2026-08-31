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
