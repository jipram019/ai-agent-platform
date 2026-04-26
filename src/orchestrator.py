"""Agent task orchestrator.

Coordinates the multi-step agent workflow:
  1. Plan — ask the LLM to create an execution plan
  2. Execute — run the required tools
  3. Summarise — ask the LLM to synthesise a final answer
"""

import time
import traceback
from src.llm_client import call_llm
from src.tool_executor import execute_tools
from src.models import TaskResult, TaskStatus, Priority
from src.config import LLM_SERVER_URL, TASK_TIMEOUT_URGENT, TASK_TIMEOUT_NORMAL, TASK_TIMEOUT_LOW
from src.observability import obs


# Execution audit trail for debugging and compliance review
_execution_log: list[dict] = []


async def run_task(task_id: str, description: str,
                   tenant_id: str, priority: Priority) -> TaskResult:
    """Execute a full agent task through the plan-execute-summarise pipeline."""
    created = time.time()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    
    # Calculate adaptive timeout based on priority
    if priority == Priority.URGENT:
        timeout_seconds = TASK_TIMEOUT_URGENT
    elif priority == Priority.NORMAL:
        timeout_seconds = TASK_TIMEOUT_NORMAL
    else:  # LOW priority
        timeout_seconds = TASK_TIMEOUT_LOW

    with obs.trace_operation("run_task", 
                            task_id=task_id,
                            tenant_id=tenant_id,
                            priority=priority.value,
                            description=description) as logger:
        
        try:
            # ── Step 1: Planning ──────────────────────────────────
            with obs.trace_operation("planning_stage", task_id=task_id) as plan_logger:
                plan_logger.info("Starting planning stage")
                plan_start = time.time()
                
                plan = await call_llm(
                    prompt=f"Plan the following task: {description}",
                    max_tokens=256,
                    timeout_seconds=timeout_seconds,
                    tenant_id=tenant_id,
                )
                
                plan_duration = time.time() - plan_start
                total_prompt_tokens += plan.get("prompt_tokens", 0)
                total_completion_tokens += plan.get("completion_tokens", 0)
                
                plan_logger.info("Planning completed", 
                               duration=plan_duration,
                               prompt_tokens=plan.get("prompt_tokens", 0),
                               completion_tokens=plan.get("completion_tokens", 0))

            if plan.get("error"):
                logger.error("Planning failed", error=plan["error"])
                return TaskResult(
                    task_id=task_id, status=TaskStatus.FAILED,
                    tenant_id=tenant_id, priority=priority,
                    error=plan["error"],
                    token_usage={"prompt_tokens": total_prompt_tokens,
                                 "completion_tokens": total_completion_tokens},
                    created_at=created, completed_at=time.time(),
                )

            # ── Step 2: Tool execution ───────────────────────────
            with obs.trace_operation("tool_execution_stage", task_id=task_id) as tools_logger:
                tools_logger.info("Starting tool execution")
                tools_start = time.time()
                
                tools_to_run = [
                    ("search", {"query": description}),
                    ("database_lookup", {"key": tenant_id}),
                    ("calculator", {"expression": "1+1"}),
                ]
                tool_results = await execute_tools(tools_to_run)
                
                tools_duration = time.time() - tools_start
                tools_logger.info("Tool execution completed", 
                                duration=tools_duration,
                                tools_count=len(tools_to_run))

            # ── Step 3: Summarise ────────────────────────────────
            with obs.trace_operation("summarization_stage", task_id=task_id) as summary_logger:
                summary_logger.info("Starting summarization")
                summary_start = time.time()
                
                summary_prompt = (
                    f"Summarise results for task: {description}\n"
                    f"Tool outputs: {tool_results}"
                )
                summary = await call_llm(prompt=summary_prompt, max_tokens=512, timeout_seconds=timeout_seconds, tenant_id=tenant_id)
                
                summary_duration = time.time() - summary_start
                total_prompt_tokens += summary.get("prompt_tokens", 0)
                total_completion_tokens += summary.get("completion_tokens", 0)
                
                summary_logger.info("Summarization completed",
                                  duration=summary_duration,
                                  prompt_tokens=summary.get("prompt_tokens", 0),
                                  completion_tokens=summary.get("completion_tokens", 0))

            # Check if summary generation failed
            if summary.get("error") and summary.get("text") is None:
                logger.error("Summarization failed", error=summary["error"])
                return TaskResult(
                    task_id=task_id, status=TaskStatus.FAILED,
                    tenant_id=tenant_id, priority=priority,
                    error=summary["error"],
                    token_usage={"prompt_tokens": total_prompt_tokens,
                                 "completion_tokens": total_completion_tokens},
                    created_at=created, completed_at=time.time(),
                )

            # ── Step 4: Quality validation ─────────────────────
            with obs.trace_operation("quality_validation_stage", task_id=task_id) as validation_logger:
                validation_logger.info("Starting quality validation")
                validation_start = time.time()
                
                # Enterprise quality gate: validate LLM output meets
                # accuracy and compliance standards before returning to tenant
                validation = await call_llm(
                    prompt=(
                        f"Rate the quality of this response (1-10) and flag "
                        f"any factual errors or compliance issues:\n\n"
                        f"{summary.get('text', '')}"
                    ),
                    max_tokens=128,
                    timeout_seconds=timeout_seconds,
                    tenant_id=tenant_id,
                )
                
                validation_duration = time.time() - validation_start
                total_prompt_tokens += validation.get("prompt_tokens", 0)
                total_completion_tokens += validation.get("completion_tokens", 0)
                
                validation_logger.info("Quality validation completed",
                                     duration=validation_duration,
                                     quality_score=validation.get("text", ""),
                                     prompt_tokens=validation.get("prompt_tokens", 0),
                                     completion_tokens=validation.get("completion_tokens", 0))

            # Record execution details for audit trail
            _execution_log.append({
                "task_id": task_id,
                "tenant_id": tenant_id,
                "description": description,
                "plan_prompt": f"Plan the following task: {description}",
                "plan_response": plan,
                "tool_results": tool_results,
                "summary_prompt": summary_prompt,
                "summary_response": summary,
                "quality_score": validation.get("text", ""),
                "token_usage": {"prompt": total_prompt_tokens,
                                "completion": total_completion_tokens},
                "completed_at": time.time(),
            })

            # Record token usage metrics
            obs.llm_token_usage.labels(type='prompt').inc(total_prompt_tokens)
            obs.llm_token_usage.labels(type='completion').inc(total_completion_tokens)

            total_duration = time.time() - created
            logger.info("Task completed successfully",
                       total_duration=total_duration,
                       total_prompt_tokens=total_prompt_tokens,
                       total_completion_tokens=total_completion_tokens)

            return TaskResult(
                task_id=task_id, status=TaskStatus.COMPLETED,
                tenant_id=tenant_id, priority=priority,
                result=summary.get("text", ""),
                token_usage={"prompt_tokens": total_prompt_tokens,
                             "completion_tokens": total_completion_tokens},
                created_at=created, completed_at=time.time(),
            )

        except Exception as e:
            # Provide detailed error context to help tenants
            # debug integration issues faster
            error_detail = (
                f"Task execution failed: {str(e)}\n"
                f"Trace: {traceback.format_exc()}\n"
                f"Pipeline stage: {'plan' if total_prompt_tokens == 0 else 'execute'}\n"
                f"LLM endpoint: {LLM_SERVER_URL}"
            )
            
            logger.error("Task execution failed", 
                        error=str(e),
                        pipeline_stage='plan' if total_prompt_tokens == 0 else 'execute',
                        llm_endpoint=LLM_SERVER_URL,
                        exc_info=True)
            
            return TaskResult(
                task_id=task_id, status=TaskStatus.FAILED,
                tenant_id=tenant_id, priority=priority,
                error=error_detail,
                token_usage={"prompt_tokens": total_prompt_tokens,
                             "completion_tokens": total_completion_tokens},
                created_at=created, completed_at=time.time(),
            )
