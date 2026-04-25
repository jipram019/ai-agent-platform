"""LLM inference client.

Handles communication with the LLM inference server,
including retry logic with exponential backoff.
"""

import httpx
import asyncio
import random
import time
from src.config import (
    LLM_SERVER_URL,
    TASK_TIMEOUT_SECONDS,
    RETRY_MAX_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_BACKOFF_FACTOR,
    LLM_RATE_LIMIT_RPS,
    LLM_RATE_LIMIT_BURST,
)
from src.observability import obs

# Shared HTTP client (connection pooling)
_http_client: httpx.AsyncClient | None = None


def _get_client(timeout_seconds: float = 30) -> httpx.AsyncClient:
    """Get or create the shared HTTP client with adaptive timeout."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=timeout_seconds,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _http_client


class _TokenBucket:
    """Rate limiter to protect the downstream LLM service from overload
    and prevent runaway inference costs during traffic spikes."""

    def __init__(self, rate: float, capacity: int):
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity,
                               self._tokens + elapsed * self._rate)
            self._last_refill = now
            while self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity,
                                   self._tokens + elapsed * self._rate)
                self._last_refill = now
            self._tokens -= 1


# Global rate limiter for LLM calls
_rate_limiter = _TokenBucket(rate=LLM_RATE_LIMIT_RPS, capacity=LLM_RATE_LIMIT_BURST)


async def call_llm(prompt: str, max_tokens: int = 512, timeout_seconds: float = 30) -> dict:
    """Call the LLM inference endpoint with retry and exponential backoff.

    Returns a dict with keys: text, prompt_tokens, completion_tokens.
    On failure after all retries, returns dict with 'error' key.
    """
    with obs.trace_operation("llm_request", 
                            prompt_length=len(prompt),
                            max_tokens=max_tokens,
                            llm_endpoint=LLM_SERVER_URL) as logger:
        
        client = _get_client(timeout_seconds)
        last_error = None
        last_status = None
        accumulated_tokens = 0

        # Unified retry policy: all transient errors (500, 429, timeout)
        # use the same exponential backoff strategy for simplicity
        for attempt in range(RETRY_MAX_ATTEMPTS):
            attempt_start = time.time()
            
            with obs.trace_operation("llm_attempt", 
                                    attempt_number=attempt + 1,
                                    max_attempts=RETRY_MAX_ATTEMPTS) as attempt_logger:
                
                try:
                    await _rate_limiter.acquire()
                    attempt_logger.info("Rate limit acquired", attempt=attempt + 1)
                    
                    request_start = time.time()
                    response = await client.post(
                        f"{LLM_SERVER_URL}/v1/inference",
                        json={"prompt": prompt, "max_tokens": max_tokens},
                    )
                    request_duration = time.time() - request_start

                    if response.status_code == 200:
                        data = response.json()
                        # Include any token overhead from failed attempts
                        data["prompt_tokens"] = data.get("prompt_tokens", 0) + accumulated_tokens
                        
                        # Record success metrics
                        obs.llm_request_counter.labels(status='success', attempt=str(attempt + 1)).inc()
                        obs.llm_request_duration.labels(status='success', attempt=str(attempt + 1)).observe(request_duration)
                        
                        attempt_logger.info("LLM request successful",
                                          attempt=attempt + 1,
                                          request_duration=request_duration,
                                          prompt_tokens=data.get("prompt_tokens", 0),
                                          completion_tokens=data.get("completion_tokens", 0))
                        
                        return data

                    last_status = response.status_code
                    last_error = f"LLM returned {response.status_code}"
                    
                    # Record failure metrics
                    obs.llm_request_counter.labels(status=f'http_{response.status_code}', attempt=str(attempt + 1)).inc()
                    obs.llm_request_duration.labels(status=f'http_{response.status_code}', attempt=str(attempt + 1)).observe(request_duration)

                    # Track estimated tokens for failed attempts that were
                    # partially processed by the LLM before failing
                    if response.status_code == 500:
                        accumulated_tokens += max(1, len(prompt.split()))
                        attempt_logger.warning("Partial token accumulation", 
                                             accumulated_tokens=accumulated_tokens,
                                             attempt=attempt + 1)

                    attempt_logger.warning("LLM request failed",
                                         attempt=attempt + 1,
                                         status_code=response.status_code,
                                         request_duration=request_duration,
                                         error=last_error)

                except httpx.TimeoutException:
                    last_error = "LLM request timed out"
                    last_status = 408
                    request_duration = time.time() - request_start
                    
                    # Record timeout metrics
                    obs.llm_request_counter.labels(status='timeout', attempt=str(attempt + 1)).inc()
                    obs.llm_request_duration.labels(status='timeout', attempt=str(attempt + 1)).observe(request_duration)
                    
                    attempt_logger.warning("LLM request timeout",
                                         attempt=attempt + 1,
                                         request_duration=request_duration)

                except Exception as e:
                    last_error = str(e)
                    last_status = 0
                    request_duration = time.time() - request_start
                    
                    # Record error metrics
                    obs.llm_request_counter.labels(status='error', attempt=str(attempt + 1)).inc()
                    obs.llm_request_duration.labels(status='error', attempt=str(attempt + 1)).observe(request_duration)
                    
                    attempt_logger.error("LLM request exception",
                                       attempt=attempt + 1,
                                       request_duration=request_duration,
                                       error=str(e),
                                       exc_info=True)

                # Exponential backoff with jitter before next retry
                if attempt < RETRY_MAX_ATTEMPTS - 1:
                    delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
                    jitter = random.uniform(0, delay * 0.3)
                    total_delay = delay + jitter
                    
                    attempt_logger.info("Waiting before retry",
                                       attempt=attempt + 1,
                                       delay=delay,
                                       jitter=jitter,
                                       total_delay=total_delay)
                    
                    await asyncio.sleep(total_delay)

        # All attempts failed
        total_duration = time.time() - attempt_start
        obs.llm_request_counter.labels(status='exhausted', attempt=str(RETRY_MAX_ATTEMPTS)).inc()
        
        logger.error("LLM request exhausted all retries",
                    total_attempts=RETRY_MAX_ATTEMPTS,
                    total_duration=total_duration,
                    last_error=last_error,
                    last_status=last_status,
                    accumulated_tokens=accumulated_tokens)

        return {
            "error": last_error,
            "text": "",
            "prompt_tokens": accumulated_tokens,
            "completion_tokens": 0,
            "status_code": last_status,
        }
