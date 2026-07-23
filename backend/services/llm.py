from config.constants import LLM_CONFIG, RATE_LIMIT_QUOTAS
import json
from typing import Dict, Any, List, Optional
import httpx
from fastapi import HTTPException
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter
from utils.circuit_breaker import circuit_breaker
from exceptions import LLMException

from config import (
    GEMINI_API_KEY,
    GEMINI_ENDPOINT,
    GEMINI_BATCH_EMBED_ENDPOINT,
    EMBEDDING_MODEL_NAME,
    logger
)

_gemini_limiter = get_quota_limiter("GEMINI", *RATE_LIMIT_QUOTAS.GEMINI)

@circuit_breaker(
    failure_threshold=5,
    recovery_timeout=60.0,
    expected_exception=Exception,
    name="gemini_llm"
)
@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def call_gemini(prompt: str) -> Dict[str, Any]:
    if not GEMINI_API_KEY:
        logger.critical("GEMINI_API_KEY not configured.")
        raise LLMException("API key not configured", recoverable=False)

    await _gemini_limiter.acquire()

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    # Initialised up front: if the request path ever exits without assigning
    # (e.g. a context manager suppresses the exception), fail with a clear
    # LLMException instead of an opaque UnboundLocalError further down.
    data = None
    try:
        async with httpx.AsyncClient(timeout=LLM_CONFIG.REQUEST_TIMEOUT) as client:
            response = await client.post(GEMINI_ENDPOINT, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error("Gemini HTTP error %s for URL %s: %s", e.response.status_code, e.request.url, e.response.text)
        raise LLMException(f"HTTP {e.response.status_code}", recoverable=True)
    except httpx.RequestError as e:
        logger.error("Gemini request error for URL %s: %s", e.request.url, str(e))
        raise LLMException(f"Request failed: {str(e)}", recoverable=True)
    except Exception as e:
        logger.exception("Unexpected error calling Gemini API.")
        raise LLMException(f"Unexpected error: {str(e)}", recoverable=False)

    if data is None:
        raise LLMException("No response payload received from Gemini", recoverable=True)

    text = ""
    try:
        if isinstance(data, dict):
            candidates = data.get("candidates", [])
            if isinstance(candidates, list) and candidates:
                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                if isinstance(parts, list) and parts:
                    text = parts[0].get("text", "")
            if not text:
                text = data.get("output", "") or data.get("text", "")
    except (AttributeError, IndexError, TypeError) as e:
        logger.error("Error parsing Gemini response structure: %s. Response: %s", e, data)
        text = json.dumps(data)
    return {"raw": data, "text": text or json.dumps(data)}


@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def get_embeddings_batch_api(texts: List[str]) -> List[Optional[List[float]]]:
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not configured. Cannot get embeddings.")
        return [None] * len(texts)
    if not texts:
        return []

    await _gemini_limiter.acquire()

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    MAX_BATCH_SIZE = 100
    all_embeddings: List[Optional[List[float]]] = []

    for i in range(0, len(texts), MAX_BATCH_SIZE):
        batch_texts = texts[i:i + MAX_BATCH_SIZE]
        requests_body = []
        for text in batch_texts:
            processed_text = text if text and text.strip() else " "
            requests_body.append({
                "model": f"models/{EMBEDDING_MODEL_NAME}",
                "content": {"parts": [{"text": processed_text}]}
            })

        body = {"requests": requests_body}
        batch_results = [None] * len(batch_texts)

        try:
            async with httpx.AsyncClient(timeout=LLM_CONFIG.BATCH_EMBED_TIMEOUT) as client:
                r = await client.post(GEMINI_BATCH_EMBED_ENDPOINT, headers=headers, json=body)
                r.raise_for_status()
                data = r.json()
                embeddings_list = data.get("embeddings", [])

                if len(embeddings_list) == len(batch_texts):
                    for j, emb_data in enumerate(embeddings_list):
                        values = emb_data.get("values")
                        if values and isinstance(values, list):
                            batch_results[j] = values
                        else:
                            logger.warning(
                                f"Received invalid or missing embedding values for text index {i+j} in batch."
                            )
                else:
                    logger.error(
                        f"Batch embedding response length mismatch in batch starting at index {i}: "
                        f"got {len(embeddings_list)}, expected {len(batch_texts)}"
                    )

        except httpx.HTTPStatusError as e:
            logger.error(
                "Gemini Batch Embed API HTTP error %s in batch starting at index %i: %s",
                e.response.status_code,
                i,
                e.response.text
            )
        except httpx.RequestError as e:
            logger.error("Gemini Batch Embed API request error in batch starting at index %i: %s", i, str(e))
        except Exception as e:
            logger.error(
                "Error calling/processing Gemini Batch Embed API for batch starting at index %i: %s",
                i,
                str(e)
            )

        all_embeddings.extend(batch_results)

    return all_embeddings
