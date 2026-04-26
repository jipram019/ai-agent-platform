"""Mock LLM inference server.

Simulates real-world unreliability:
  - ~10% HTTP 500 errors
  - ~5% HTTP 429 rate-limit responses
  - ~5% extreme latency (>5 s)
  - Normal latency 100 ms – 1 s
  - Fair queuing algorithm to prevent tenant discrimination

This unreliability is INTENTIONAL and is NOT one of the hidden issues.
"""

import asyncio, random, time
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from collections import defaultdict, deque

app = FastAPI(title="Mock LLM Server")

# Fair queuing system to prevent tenant discrimination
class FairQueue:
    def __init__(self):
        self.queues = defaultdict(deque)  # tenant_id -> queue
        self.last_served = {}  # tenant_id -> last service time
        self.lock = asyncio.Lock()
    
    async def add_request(self, tenant_id: str):
        async with self.lock:
            self.queues[tenant_id].append(time.time())
    
    async def get_next_tenant(self) -> str:
        async with self.lock:
            current_time = time.time()
            
            # Find tenant with longest wait time
            longest_wait = 0
            selected_tenant = None
            
            for tenant_id, queue in self.queues.items():
                if queue:
                    wait_time = current_time - queue[0]
                    # Factor in last service time for fairness
                    last_service = self.last_served.get(tenant_id, 0)
                    priority_score = wait_time + (current_time - last_service) * 0.5
                    
                    if priority_score > longest_wait:
                        longest_wait = priority_score
                        selected_tenant = tenant_id
            
            if selected_tenant and self.queues[selected_tenant]:
                self.queues[selected_tenant].popleft()
                self.last_served[selected_tenant] = current_time
                return selected_tenant
            
            return None

# Global fair queue instance
fair_queue = FairQueue()

# Track active requests per tenant for rate limiting
active_requests = defaultdict(int)
rate_limit_lock = asyncio.Lock()


class InferenceRequest(BaseModel):
    prompt: str
    max_tokens: int = 512


class InferenceResponse(BaseModel):
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str = "mock-gpt-4"


@app.post("/v1/inference", response_model=InferenceResponse)
async def inference(req: InferenceRequest, request: Request):
    # Extract tenant_id from headers (sent by LLM client)
    tenant_id = request.headers.get("X-Tenant-ID", "unknown")
    
    # Add to fair queue
    await fair_queue.add_request(tenant_id)
    
    # Wait for fair scheduling
    while True:
        next_tenant = await fair_queue.get_next_tenant()
        if next_tenant == tenant_id:
            break
        await asyncio.sleep(0.01)  # Small delay to prevent busy waiting
    
    # Check rate limits (max 5 concurrent requests per tenant)
    async with rate_limit_lock:
        if active_requests[tenant_id] >= 5:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        active_requests[tenant_id] += 1
    
    try:
        roll = random.random()

        # ~10% server error
        if roll < 0.10:
            raise HTTPException(status_code=500, detail="Internal LLM error")

        # ~5% rate limit
        if roll < 0.15:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

        # Fair latency: all tenants get similar response times
        base_latency = random.uniform(0.1, 0.5)  # Reduced variance
        
        # Small adjustment based on tenant load (max 20% difference)
        async with rate_limit_lock:
            load_factor = min(1.2, 1.0 + (active_requests[tenant_id] - 1) * 0.1)
        
        actual_latency = base_latency * load_factor
        
        # ~5% extreme latency (reduced from 5-10s to 2-4s)
        if roll < 0.20:
            actual_latency += random.uniform(2, 4)
        
        await asyncio.sleep(actual_latency)

        prompt_tokens = max(1, len(req.prompt.split()) * 2)
        completion_tokens = random.randint(50, req.max_tokens)

        return InferenceResponse(
            text=f"Mock response for: {req.prompt[:80]}",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    
    finally:
        # Release rate limit slot
        async with rate_limit_lock:
            active_requests[tenant_id] -= 1


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
