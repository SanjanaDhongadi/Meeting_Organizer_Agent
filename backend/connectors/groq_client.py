"""
The single place that talks to the AI provider (Groq). Every LLM feature (request extraction, agenda refinement,
minutes generation) and the audio transcription adapter use this module; no other file creates an API client.

Errors are returned as `LLMError` with a stable `code`, a message that never contains the API key, and a
`retryable` flag. Transient failures (rate limits, timeouts, network, 5xx) are retried a few times with back-off;
nothing is retried indefinitely.
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger("groq")

MAX_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 20.0


class LLMError(RuntimeError):
    """code: NOT_CONFIGURED, AUTH, RATE_LIMIT, QUOTA, TIMEOUT, NETWORK, MODEL_UNSUPPORTED, BAD_REQUEST, PARSE, SERVER, ERROR."""

    def __init__(self, code: str, message: str, retryable: bool = False, retry_after: Optional[float] = None):
        super().__init__(message)
        self.code, self.retryable, self.retry_after = code, retryable, retry_after


def is_configured() -> bool:
    return bool((settings.GROQ_API_KEY or "").strip()) and (settings.AI_PROVIDER or "groq").lower() == "groq"


def not_configured_message(feature: str = "AI features") -> str:
    if (settings.AI_PROVIDER or "groq").lower() != "groq":
        return f"AI_PROVIDER={settings.AI_PROVIDER!r} is not supported; only 'groq' is. {feature} are unavailable."
    return f"GROQ_API_KEY is not configured, so {feature} are unavailable. Add your Groq key to .env."


def _base_url() -> str:
    """The Groq SDK appends /openai/v1 itself; accept the OpenAI-compatible URL form used in .env as well."""
    url = (settings.GROQ_BASE_URL or "https://api.groq.com").rstrip("/")
    return re.sub(r"/openai/v1$", "", url)


def get_client(timeout: float = 120.0):
    if not is_configured():
        raise LLMError("NOT_CONFIGURED", not_configured_message())
    from groq import Groq
    return Groq(api_key=settings.GROQ_API_KEY.strip(), base_url=_base_url(), timeout=timeout, max_retries=0)


def classify_error(error: Exception) -> LLMError:
    """Map an SDK/network exception to an LLMError. The original text is trimmed and the key is never echoed."""
    if isinstance(error, LLMError):
        return error
    name = type(error).__name__
    status = getattr(error, "status_code", None)
    text = str(error)
    lowered = text.lower()
    retry_after = None
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers:
        try:
            retry_after = float(headers.get("retry-after"))
        except (TypeError, ValueError):
            retry_after = None
    safe = text.replace(settings.GROQ_API_KEY or "\0", "***")[:300]

    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return LLMError("AUTH", "The Groq API key was rejected. Check GROQ_API_KEY in .env.")
    if status == 429 or name == "RateLimitError":
        if any(w in lowered for w in ("per day", "quota", "billing", "exceeded your current")):
            return LLMError("QUOTA", "The Groq usage quota is used up for now. Wait for the limit to reset or upgrade the plan.")
        return LLMError("RATE_LIMIT", "Groq is rate limiting requests. Try again shortly.", True, retry_after)
    if name == "APITimeoutError" or isinstance(error, TimeoutError):
        return LLMError("TIMEOUT", "The request to Groq timed out.", True)
    if name == "APIConnectionError" or isinstance(error, (ConnectionError, OSError)):
        return LLMError("NETWORK", "Could not reach Groq. Check the network connection.", True)
    if isinstance(status, int) and status >= 500:
        return LLMError("SERVER", f"Groq had a server error (HTTP {status}).", True)
    if status == 404 or "model_not_found" in lowered or "decommissioned" in lowered or "does not exist" in lowered:
        return LLMError("MODEL_UNSUPPORTED", f"The configured Groq model is not available: {safe}")
    if status == 400 or name == "BadRequestError":
        return LLMError("BAD_REQUEST", f"Groq rejected the request: {safe}")
    return LLMError("ERROR", f"The AI request failed ({name}): {safe}")


def call_with_retries(fn, attempts: int = MAX_ATTEMPTS):
    """Run fn(); retry transient failures with exponential back-off, honouring Retry-After (capped)."""
    last: Optional[LLMError] = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:
            last = classify_error(error)
            if not last.retryable or attempt == attempts - 1:
                break
            delay = min(last.retry_after if last.retry_after else 2 ** attempt, MAX_BACKOFF_SECONDS)
            logger.warning("Groq call failed (%s); retrying in %.0fs", last.code, delay)
            time.sleep(delay)
    raise last


def parse_json_object(content: str) -> Dict[str, Any]:
    """Parse a model reply as a JSON object, tolerating code fences or reasoning text around it."""
    text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL).strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("PARSE", "The model did not return valid JSON.")
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            raise LLMError("PARSE", "The model did not return valid JSON.")
    if not isinstance(value, dict):
        raise LLMError("PARSE", "The model returned JSON that is not an object.")
    return value


def chat_json(system: Optional[str], user: str, temperature: float = 0.0) -> Dict[str, Any]:
    """One chat completion in JSON mode, returned as a dict. Raises LLMError (never returns made-up content)."""
    client = get_client()
    messages: List[Dict[str, str]] = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]

    def once() -> Dict[str, Any]:
        from runtime.governance import count_llm_call  # lazy: governance imports LLMError from this module
        count_llm_call()   # budget check before every actual request (retries included); no-op outside a governed run
        response = client.chat.completions.create(
            model=settings.GROQ_LLM_MODEL, messages=messages, response_format={"type": "json_object"}, temperature=temperature)
        return parse_json_object(response.choices[0].message.content)

    for parse_attempt in range(2):          # a malformed reply is worth exactly one more try
        try:
            return call_with_retries(once)
        except LLMError as error:
            if error.code != "PARSE" or parse_attempt == 1:
                raise
    raise LLMError("PARSE", "The model did not return valid JSON.")
