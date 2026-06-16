"""
llm_client.py
=============
Thin shared wrapper around the OpenRouter API (OpenAI-compatible
/chat/completions endpoint). Every LLM-calling module in this project
(relevance_filter.py, deep_scorer.py, query generation, etc.) goes through
this one client so the API key, retry logic, and JSON-parsing live in
exactly one place.

Models used in this pipeline (all free tier on OpenRouter):
    QUERY_GEN_MODEL   -> meta-llama/llama-3.3-70b-instruct:free
    PREFILTER_MODEL   -> meta-llama/llama-3.2-3b-instruct:free
    DEEP_SCORE_MODEL  -> nvidia/nemotron-3-super-120b:free  (also used for briefing)

Free-tier note: OpenRouter free accounts are capped at roughly 200
requests/day combined and ~20 requests/minute per model. This module does
not attempt to manage that budget itself — callers should batch sensibly
(see relevance_filter.py for an example of keeping prompts short so a
high request volume is feasible within the cap).
"""

import json
import os
import time

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

QUERY_GEN_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
PREFILTER_MODEL = "meta-llama/llama-3.2-3b-instruct:free"
DEEP_SCORE_MODEL = "nvidia/nemotron-3-super-120b:free"

DEFAULT_TIMEOUT = 60


class LLMError(Exception):
    pass


def _api_key():
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise LLMError(
            "OPENROUTER_API_KEY is not set. Add it to your .env file."
        )
    return key


def call_llm(
    model: str,
    system_prompt: str,
    user_prompt: str,
    json_mode: bool = True,
    max_tokens: int = 1000,
    temperature: float = 0.1,
    retries: int = 3,
    base_delay: float = 2.0,
) -> dict:
    """
    Calls OpenRouter's chat completions endpoint and returns a dict:
        {"ok": True, "text": <raw text>, "parsed": <dict or None>}
    or on failure:
        {"ok": False, "error": <message>}

    json_mode=True asks the model to return JSON only and attempts to
    parse the response. If parsing fails, "parsed" is None but "text"
    still contains the raw model output so the caller can decide what
    to do (retry, log, fall back).
    """
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_error = None

    for attempt in range(retries):
        try:
            resp = requests.post(
                OPENROUTER_URL, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT
            )

            if resp.status_code == 429:
                wait = base_delay * (2 ** attempt)
                time.sleep(wait)
                last_error = "Rate limited (429)."
                continue

            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                # Don't retry on 4xx other than 429 (bad request, auth, etc.)
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    break
                time.sleep(base_delay * (2 ** attempt))
                continue

            data = resp.json()
            choice = data["choices"][0]["message"]["content"]

            parsed = None
            if json_mode:
                parsed = _try_parse_json(choice)

            return {"ok": True, "text": choice, "parsed": parsed}

        except requests.exceptions.Timeout:
            last_error = "Request timed out."
            time.sleep(base_delay * (2 ** attempt))
        except requests.exceptions.RequestException as e:
            last_error = f"Network error: {e}"
            time.sleep(base_delay * (2 ** attempt))
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            last_error = f"Unexpected response shape: {e}"
            break

    return {"ok": False, "error": last_error or "Unknown LLM call failure."}


def _try_parse_json(text: str):
    """
    Models sometimes wrap JSON in markdown fences even when asked not to.
    Strip those before parsing, and try to recover if there's leading/
    trailing prose around the JSON object.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Last resort: grab the substring between the first { and last }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None

    return None