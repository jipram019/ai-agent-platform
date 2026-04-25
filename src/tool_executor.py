"""Simulated tool execution layer.

Each tool simulates an external service call (search engine,
database, calculator, etc.) with realistic latency.
"""

import asyncio
import random
import time
from src.observability import obs


async def execute_tool(tool_name: str, args: dict) -> dict:
    """Execute a single tool and return its result."""
    start_time = time.time()
    
    with obs.trace_operation("execute_tool", 
                            tool_name=tool_name,
                            args=args) as logger:
        
        # Simulate variable latency per tool type
        latency_map = {
            "search": (0.1, 0.5),
            "database_lookup": (0.05, 0.2),
            "calculator": (0.01, 0.05),
        }
        low, high = latency_map.get(tool_name, (0.05, 0.3))
        simulated_delay = random.uniform(low, high)
        
        logger.info("Tool execution started", 
                   tool_name=tool_name,
                   expected_delay_range=(low, high))
        
        await asyncio.sleep(simulated_delay)
        
        duration = time.time() - start_time
        
        # Record metrics
        obs.tool_execution_counter.labels(tool_name=tool_name, status='success').inc()
        obs.tool_execution_duration.labels(tool_name=tool_name).observe(duration)
        
        result = {
            "tool": tool_name,
            "status": "success",
            "output": f"Result from {tool_name}",
        }
        
        logger.info("Tool execution completed",
                   tool_name=tool_name,
                   duration=duration,
                   simulated_delay=simulated_delay)
        
        return result


async def execute_tools(tools: list[tuple[str, dict]]) -> list[dict]:
    """Execute multiple tools and return results in order.

    Args:
        tools: List of (tool_name, args) tuples to execute.

    Returns:
        Ordered list of tool execution results.
    """
    start_time = time.time()
    
    with obs.trace_operation("execute_tools", 
                            tools_count=len(tools),
                            tool_names=[tool_name for tool_name, _ in tools]) as logger:
        
        logger.info("Batch tool execution started", tools_count=len(tools))
        
        results = []
        for tool_name, args in tools:
            result = await execute_tool(tool_name, args)
            results.append(result)
        
        duration = time.time() - start_time
        logger.info("Batch tool execution completed", 
                   tools_count=len(tools),
                   total_duration=duration)
        
        return results
