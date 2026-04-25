"""FastAPI application — Agent Execution Service.

Provides the HTTP API for submitting and querying agent tasks.
"""

import uuid
import time
import asyncio
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from src.models import Priority, TaskStatus, TaskResult
from src.orchestrator import run_task
from src.config import MAX_CONCURRENT_TASKS, TASK_TIMEOUT_SECONDS
from src.observability import obs

app = FastAPI(title="Agent Execution Service")

# Initialize observability instrumentation
obs.instrument_fastapi(app)

# Task storage
task_store: dict[str, TaskResult] = {}

# Response cache for repeated queries — avoids redundant LLM calls
_response_cache: dict[str, dict] = {}

# Limit concurrent task executions to protect downstream LLM service
_task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Ensure tasks for the same tenant execute in submission order
# to prevent race conditions on downstream tenant state
_tenant_locks: dict[str, asyncio.Lock] = {}


class CreateTaskBody(BaseModel):
    task_description: str
    tenant_id: str
    priority: Priority = Priority.NORMAL


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    tenant_id: str
    priority: Priority
    result: Optional[str] = None
    error: Optional[str] = None
    token_usage: Optional[dict] = None
    created_at: Optional[float] = None
    completed_at: Optional[float] = None


@app.post("/tasks", response_model=TaskResponse)
async def create_task(body: CreateTaskBody, request: Request):
    start_time = time.time()
    
    with obs.trace_operation("create_task", 
                            tenant_id=body.tenant_id, 
                            priority=body.priority.value,
                            task_description=body.task_description) as logger:
        
        task_id = str(uuid.uuid4())
        logger.info("Creating task", task_id=task_id)

        # Cache key: tenant + description (priority excluded because
        # task results are priority-independent in the current design)
        cache_key = f"{body.tenant_id}:{body.task_description}"
        if cache_key in _response_cache:
            logger.info("Cache hit", cache_key=cache_key)
            obs.cache_hits.labels(cache_type='response').inc()
            
            cached = _response_cache[cache_key]
            result = TaskResult(
                task_id=task_id, status=TaskStatus.COMPLETED,
                tenant_id=body.tenant_id, priority=body.priority,
                result=cached.get("result"),
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                created_at=time.time(), completed_at=time.time(),
            )
            task_store[task_id] = result
            
            # Record metrics
            duration = time.time() - start_time
            obs.request_counter.labels(
                method='POST', endpoint='/tasks', status='200',
                tenant_id=body.tenant_id, priority=body.priority.value
            ).inc()
            obs.request_duration.labels(
                method='POST', endpoint='/tasks',
                tenant_id=body.tenant_id, priority=body.priority.value
            ).observe(duration)
            obs.task_counter.labels(
                status='completed', tenant_id=body.tenant_id, priority=body.priority.value
            ).inc()
            
            return _to_response(result)

        # Execute the task (bounded by concurrency limit)
        task_store[task_id] = TaskResult(
            task_id=task_id, status=TaskStatus.PENDING,
            tenant_id=body.tenant_id, priority=body.priority,
        )
        obs.active_tasks.inc()

        async def _guarded_execute():
            lock = _tenant_locks.setdefault(body.tenant_id, asyncio.Lock())
            async with lock:
                async with _task_semaphore:
                    return await run_task(
                        task_id=task_id,
                        description=body.task_description,
                        tenant_id=body.tenant_id,
                        priority=body.priority,
                    )

        # Adaptive timeout based on task priority
        if body.priority == "urgent":
            timeout_seconds = TASK_TIMEOUT_URGENT
        elif body.priority == "normal":
            timeout_seconds = TASK_TIMEOUT_NORMAL
        else:  # low priority
            timeout_seconds = TASK_TIMEOUT_LOW
            
        # Enforce task-level deadline: clients should not wait indefinitely
        try:
            result = await asyncio.wait_for(
                _guarded_execute(), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.error("Task timeout", task_id=task_id, timeout=timeout_seconds, priority=body.priority)
            result = TaskResult(
                task_id=task_id, status=TaskStatus.FAILED,
                tenant_id=body.tenant_id, priority=body.priority,
                error="Task execution exceeded time limit",
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                created_at=time.time(), completed_at=time.time(),
            )
        finally:
            obs.active_tasks.dec()
        
        task_store[task_id] = result

        # Cache successful responses for future identical requests
        if result.status == TaskStatus.COMPLETED:
            _response_cache[cache_key] = {"result": result.result}
            logger.info("Response cached", cache_key=cache_key)

        # Record metrics
        duration = time.time() - start_time
        status_code = '200' if result.status == TaskStatus.COMPLETED else '500'
        obs.request_counter.labels(
            method='POST', endpoint='/tasks', status=status_code,
            tenant_id=body.tenant_id, priority=body.priority.value
        ).inc()
        obs.request_duration.labels(
            method='POST', endpoint='/tasks',
            tenant_id=body.tenant_id, priority=body.priority.value
        ).observe(duration)
        obs.task_counter.labels(
            status=result.status.value, tenant_id=body.tenant_id, priority=body.priority.value
        ).inc()
        
        if result.status == TaskStatus.COMPLETED:
            obs.task_duration.labels(
                tenant_id=body.tenant_id, priority=body.priority.value, status='completed'
            ).observe(duration)
        else:
            obs.task_duration.labels(
                tenant_id=body.tenant_id, priority=body.priority.value, status='failed'
            ).observe(duration)

        logger.info("Task completed", 
                   task_id=task_id, 
                   status=result.status.value,
                   duration=duration)

        return _to_response(result)


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    start_time = time.time()
    
    with obs.trace_operation("get_task", task_id=task_id) as logger:
        if task_id not in task_store:
            logger.warning("Task not found", task_id=task_id)
            raise HTTPException(status_code=404, detail="Task not found")
        
        result = task_store[task_id]
        
        # Record metrics
        duration = time.time() - start_time
        obs.request_counter.labels(
            method='GET', endpoint='/tasks/{task_id}', status='200',
            tenant_id=result.tenant_id, priority=result.priority.value
        ).inc()
        obs.request_duration.labels(
            method='GET', endpoint='/tasks/{task_id}',
            tenant_id=result.tenant_id, priority=result.priority.value
        ).observe(duration)
        
        logger.info("Task retrieved", 
                   task_id=task_id, 
                   status=result.status.value)
        
        return _to_response(result)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return obs.get_metrics_response()


def _to_response(r: TaskResult) -> TaskResponse:
    return TaskResponse(
        task_id=r.task_id, status=r.status,
        tenant_id=r.tenant_id, priority=r.priority,
        result=r.result, error=r.error,
        token_usage=r.token_usage,
        created_at=r.created_at, completed_at=r.completed_at,
    )
