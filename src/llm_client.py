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


class _CircuitBreaker:
    """Circuit breaker pattern to prevent cascading failures during LLM outages.
    
    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: All requests fail immediately
    - HALF_OPEN: Limited requests allowed to test recovery
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 30, 
                 half_open_max_calls: int = 3):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.half_open_calls = 0
        self._lock = asyncio.Lock()

    async def call(self, func, *args, **kwargs):
        """Execute function with circuit breaker protection."""
        async with self._lock:
            if self.state == "OPEN":
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = "HALF_OPEN"
                    self.half_open_calls = 0
                    obs.circuit_breaker_state.labels(state="half_open").inc()
                else:
                    obs.circuit_breaker_state.labels(state="open").inc()
                    raise Exception("Circuit breaker is OPEN")

            if self.state == "HALF_OPEN":
                if self.half_open_calls >= self.half_open_max_calls:
                    obs.circuit_breaker_state.labels(state="half_open_reject").inc()
                    raise Exception("Circuit breaker HALF_OPEN limit reached")
                self.half_open_calls += 1

        try:
            result = await func(*args, **kwargs)
            
            async with self._lock:
                if self.state == "HALF_OPEN":
                    if self.half_open_calls >= self.half_open_max_calls:
                        self.state = "CLOSED"
                        self.failure_count = 0
                        obs.circuit_breaker_state.labels(state="closed").inc()
            
            return result
            
        except Exception as e:
            async with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.time()
                
                if self.failure_count >= self.failure_threshold:
                    self.state = "OPEN"
                    obs.circuit_breaker_state.labels(state="open").inc()
                elif self.state == "HALF_OPEN":
                    self.state = "OPEN"
                    obs.circuit_breaker_state.labels(state="open").inc()
                    
            raise e


class _IntelligentTokenBucket:
    """Intelligent rate limiter with priority-based burst capacity and token bucket algorithm.
    
    Implements different rate limits based on task priority:
    - Urgent tasks: 2x burst capacity, faster refill rate
    - Normal tasks: standard burst capacity and refill rate
    - Low priority: standard burst capacity, slower refill rate
    """

    def __init__(self, base_rate: float, base_capacity: int):
        self.base_rate = base_rate
        self.base_capacity = base_capacity
        self._buckets = {}  # tenant_id -> _TenantBucket
        self._lock = asyncio.Lock()

    def _get_priority_multiplier(self, tenant_id: str) -> tuple[float, int]:
        """Get rate and capacity multipliers based on tenant priority pattern."""
        # Extract priority from tenant ID or use default
        if "urgent" in tenant_id.lower() or tenant_id.endswith("-alpha"):
            return 2.0, self.base_capacity * 2  # 2x rate, 2x burst for urgent
        elif "low" in tenant_id.lower() or tenant_id.endswith("-gamma"):
            return 0.8, self.base_capacity  # 0.8x rate, standard burst for low
        else:
            return 1.0, self.base_capacity  # Standard rate and burst for normal

    async def acquire(self, tenant_id: str = "unknown"):
        async with self._lock:
            if tenant_id not in self._buckets:
                rate_mult, capacity_mult = self._get_priority_multiplier(tenant_id)
                self._buckets[tenant_id] = _TenantBucket(
                    rate=self.base_rate * rate_mult,
                    capacity=self.base_capacity * capacity_mult,
                    tenant_id=tenant_id
                )
            
            await self._buckets[tenant_id].acquire()


class _TenantBucket:
    """Per-tenant token bucket with individual rate limiting."""

    def __init__(self, rate: float, capacity: int, tenant_id: str):
        self.rate = rate
        self.capacity = capacity
        self.tenant_id = tenant_id
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity,
                               self._tokens + elapsed * self.rate)
            self._last_refill = now
            
            while self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                await asyncio.sleep(wait)
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.capacity,
                                   self._tokens + elapsed * self.rate)
                self._last_refill = now
            self._tokens -= 1


# Global intelligent rate limiter for LLM calls
_rate_limiter = _IntelligentTokenBucket(
    base_rate=LLM_RATE_LIMIT_RPS,
    base_capacity=LLM_RATE_LIMIT_BURST,
)

# Global circuit breaker for LLM service protection
_circuit_breaker = _CircuitBreaker(failure_threshold=5, recovery_timeout=30, half_open_max_calls=3)


async def call_llm(prompt: str, max_tokens: int = 512, timeout_seconds: float = 30, tenant_id: str = "unknown") -> dict:
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
                    async def make_llm_request():
                        await _rate_limiter.acquire(tenant_id)
                        attempt_logger.info("Rate limit acquired", attempt=attempt + 1, tenant_id=tenant_id)
                        
                        request_start = time.time()
                        response = await client.post(
                            f"{LLM_SERVER_URL}/v1/inference",
                            json={"prompt": prompt, "max_tokens": max_tokens},
                            headers={"X-Tenant-ID": tenant_id}
                        )
                        request_duration = time.time() - request_start
                        return response, request_duration
                    
                    # Wrap the LLM request with circuit breaker
                    response, request_duration = await _circuit_breaker.call(make_llm_request)

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
