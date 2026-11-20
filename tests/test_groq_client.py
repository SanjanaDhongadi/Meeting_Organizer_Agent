"""The Groq integration: one central client, safe errors, bounded retries. No network: the SDK call is stubbed."""
import json
import types

import pytest

from backend.agents.coordinator_agent import MeetingCoordinator
from backend.agents.mom_agent import MoMAgent, MoMUnavailable
from backend.app.config import settings
from backend.connectors import groq_client
from backend.connectors.groq_client import LLMError
from backend.connectors.transcription_service import TranscriptionService

KEY = "gsk_secret_value_123"


class ApiError(Exception):
    def __init__(self, status, message="boom", headers=None):
        super().__init__(message)
        self.status_code = status
        self.response = types.SimpleNamespace(headers=headers or {})


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "GROQ_API_KEY", KEY)
    monkeypatch.setattr(settings, "AI_PROVIDER", "groq")
    monkeypatch.setattr(groq_client.time, "sleep", lambda s: None)


def _client(*replies):
    """A stand-in Groq SDK client: each reply is a JSON string, or an exception to raise."""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=reply))])

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))), calls


def _use(monkeypatch, client):
    monkeypatch.setattr(groq_client, "get_client", lambda timeout=120.0: client)


def test_configuration_and_base_url(monkeypatch):
    assert groq_client.is_configured()
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    assert not groq_client.is_configured()
    with pytest.raises(LLMError) as err:
        groq_client.chat_json("s", "u")
    assert err.value.code == "NOT_CONFIGURED" and "GROQ_API_KEY" in str(err.value)
    monkeypatch.setattr(settings, "GROQ_API_KEY", KEY)
    monkeypatch.setattr(settings, "AI_PROVIDER", "other")
    assert not groq_client.is_configured() and "not supported" in groq_client.not_configured_message()
    for url in ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1/", "https://api.groq.com"):
        monkeypatch.setattr(settings, "GROQ_BASE_URL", url)
        assert groq_client._base_url() == "https://api.groq.com"        # the SDK adds /openai/v1 itself


def test_sdk_client_is_built_from_central_settings(monkeypatch):
    import groq
    seen = {}
    monkeypatch.setattr(groq, "Groq", lambda **kw: seen.update(kw) or object())
    groq_client.get_client()
    assert seen["api_key"] == KEY and seen["base_url"] == "https://api.groq.com" and seen["max_retries"] == 0


def test_chat_json_uses_the_configured_model_in_json_mode(monkeypatch):
    client, calls = _client('{"ok": true}')
    _use(monkeypatch, client)
    assert groq_client.chat_json("system", "user") == {"ok": True}
    assert calls[0]["model"] == settings.GROQ_LLM_MODEL == "openai/gpt-oss-120b"
    assert calls[0]["response_format"] == {"type": "json_object"} and calls[0]["temperature"] == 0
    assert [m["role"] for m in calls[0]["messages"]] == ["system", "user"]


def test_json_in_fences_or_reasoning_text_is_parsed_and_garbage_is_rejected(monkeypatch):
    assert groq_client.parse_json_object('<think>hmm</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert groq_client.parse_json_object('Here you go: {"a": [1, 2]} thanks') == {"a": [1, 2]}
    for bad in ("not json", "[1, 2]", ""):
        with pytest.raises(LLMError) as err:
            groq_client.parse_json_object(bad)
        assert err.value.code == "PARSE"
    client, calls = _client("garbage", '{"fixed": 1}')
    _use(monkeypatch, client)
    assert groq_client.chat_json(None, "u") == {"fixed": 1} and len(calls) == 2     # one more try for a malformed reply
    client, calls = _client("garbage")
    _use(monkeypatch, client)
    with pytest.raises(LLMError):
        groq_client.chat_json(None, "u")
    assert len(calls) == 2                                                           # but not forever


def test_transient_errors_are_retried_a_bounded_number_of_times(monkeypatch):
    client, calls = _client(ApiError(429, "slow down"), ApiError(503), '{"ok": 1}')
    _use(monkeypatch, client)
    assert groq_client.chat_json(None, "u") == {"ok": 1} and len(calls) == 3
    client, calls = _client(ApiError(429, "slow down"))
    _use(monkeypatch, client)
    with pytest.raises(LLMError) as err:
        groq_client.chat_json(None, "u")
    assert err.value.code == "RATE_LIMIT" and len(calls) == groq_client.MAX_ATTEMPTS


@pytest.mark.parametrize("error, code, retryable", [
    (ApiError(401, "Invalid API Key " + KEY), "AUTH", False),
    (ApiError(403), "AUTH", False),
    (ApiError(429, "Rate limit reached for tokens per day (TPD)"), "QUOTA", False),
    (ApiError(429, "tokens per minute"), "RATE_LIMIT", True),
    (ApiError(404, "The model `x` does not exist"), "MODEL_UNSUPPORTED", False),
    (ApiError(400, "bad"), "BAD_REQUEST", False),
    (ApiError(500), "SERVER", True),
    (TimeoutError("t"), "TIMEOUT", True),
    (ConnectionError("c"), "NETWORK", True),
])
def test_errors_are_classified_and_never_leak_the_key(error, code, retryable):
    problem = groq_client.classify_error(error)
    assert problem.code == code and problem.retryable is retryable
    assert KEY not in str(problem)


def test_non_retryable_errors_are_not_retried(monkeypatch):
    client, calls = _client(ApiError(401, "bad key"))
    _use(monkeypatch, client)
    with pytest.raises(LLMError) as err:
        groq_client.chat_json(None, "u")
    assert err.value.code == "AUTH" and len(calls) == 1


def test_retry_after_header_is_honoured_but_capped(monkeypatch):
    waits = []
    monkeypatch.setattr(groq_client.time, "sleep", waits.append)
    client, _ = _client(ApiError(429, "x", headers={"retry-after": "500"}), '{"ok": 1}')
    _use(monkeypatch, client)
    groq_client.chat_json(None, "u")
    assert waits == [groq_client.MAX_BACKOFF_SECONDS]


# ----- features that use it -----

def test_mom_agent_goes_through_groq_and_reports_failures(monkeypatch):
    client, calls = _client(json.dumps({"summary": "s"}))
    _use(monkeypatch, client)
    assert MoMAgent()._complete("sys", "usr") == {"summary": "s"}
    client, _ = _client(ApiError(401, "no"))
    _use(monkeypatch, client)
    with pytest.raises(MoMUnavailable) as err:
        MoMAgent()._complete("sys", "usr")
    assert "AUTH" in str(err.value) and KEY not in str(err.value)
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    with pytest.raises(MoMUnavailable) as err:
        MoMAgent()._complete("sys", "usr")
    assert "GROQ_API_KEY" in str(err.value)


def test_request_extraction_and_agenda_use_groq_when_live(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    reply = json.dumps({"participants": ["Nagveni"], "title": "Launch sync", "purpose": "Review the launch", "date": "2026-12-01",
                        "start_time": "14:00", "duration_minutes": 30, "mode": "ONLINE",
                        "agenda_points": ["1. budget", "2. hiring"]})
    client, calls = _client(reply)
    _use(monkeypatch, client)
    parsed = MeetingCoordinator().parse_natural_language_request(
        "Online meeting with Nagveni on 2026-12-01 at 2pm for 30 minutes. Agenda: budget; hiring")
    assert parsed["parser"] == "groq" and parsed["title"] == "Launch sync" and parsed["agenda_points"] == ["Budget", "Hiring"]
    assert len(calls) == 1

    from backend.skills.agenda_skill import improve_agenda
    client, calls = _client(json.dumps({"agenda_points": ["Review the proposed budget.", "Discuss the hiring plan."]}))
    _use(monkeypatch, client)
    points, text = improve_agenda(["budget", "hiring"], purpose="Launch", title="Sync", raw_request="x")
    assert points == ["Review the proposed budget.", "Discuss the hiring plan."] and text.startswith("1. ")
    assert calls[0]["model"] == settings.GROQ_LLM_MODEL


def test_live_extraction_falls_back_to_rules_when_groq_fails(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    client, _ = _client(ApiError(401, "bad key"))
    _use(monkeypatch, client)
    parsed = MeetingCoordinator().parse_natural_language_request(
        "Online meeting with Nagveni tomorrow at 2pm for 30 minutes. Agenda: budget; hiring")
    assert parsed["parser"] == "rules" and parsed["agenda_points"] == ["Budget", "Hiring"]


# ----- transcription -----

def _audio(tmp_path, size=10):
    path = tmp_path / "a.webm"
    path.write_bytes(b"x" * size)
    return str(path)


def _stt_service(monkeypatch, create):
    service = TranscriptionService()
    stub = types.SimpleNamespace(audio=types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(service, "_client", lambda: stub)
    return service


def test_transcription_uses_groq_whisper_and_validates_the_file(tmp_path, monkeypatch):
    seen = {}

    def create(**kw):
        seen.update(kw)
        return types.SimpleNamespace(text="x", segments=[{"start": 1.0, "text": " Hello. "}])

    service = _stt_service(monkeypatch, create)
    result = service.transcribe_file(_audio(tmp_path), "audio/webm;codecs=opus", 60)
    assert result["success"] and result["text"] == "[00:01:01] Speaker: Hello."
    assert seen["model"] == "whisper-large-v3" and seen["response_format"] == "verbose_json" and seen["file"][0] == "segment.webm"

    unsupported = service.transcribe_file(_audio(tmp_path), "video/avi")
    assert not unsupported["success"] and not unsupported["retryable"] and unsupported["code"] == "TRANSCRIPTION_UNSUPPORTED_FORMAT"
    monkeypatch.setattr(settings, "RECORDING_MAX_SEGMENT_MB", 0)
    big = service.transcribe_file(_audio(tmp_path), "audio/webm")
    assert big["code"] == "TRANSCRIPTION_TOO_LARGE" and not big["retryable"]


@pytest.mark.parametrize("status, retryable", [(429, True), (503, True), (401, False)])
def test_transcription_failures_are_classified(tmp_path, monkeypatch, status, retryable):
    def create(**kw):
        raise ApiError(status, "x " + KEY)

    result = _stt_service(monkeypatch, create).transcribe_file(_audio(tmp_path), "audio/webm")
    assert not result["success"] and result["retryable"] is retryable and KEY not in result["error"]
