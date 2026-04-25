"""Centralized observability configuration for the agent platform."""

import logging
import structlog
from contextlib import contextmanager
from typing import Dict, Any, Optional
from opentelemetry import trace, metrics
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi import Response


class ObservabilityConfig:
    """Central configuration for all observability components."""
    
    def __init__(self, service_name: str = "agent-service"):
        self.service_name = service_name
        self.tracer = None
        self.meter = None
        self.logger = None
        self._instrumented = False
        self._setup_telemetry()
        self._setup_logging()
        self._setup_metrics()
    
    def _setup_telemetry(self):
        """Initialize OpenTelemetry tracing and metrics."""
        # Set up resource
        resource = Resource(attributes={
            SERVICE_NAME: self.service_name,
        })
        
        # Set up tracing
        trace_provider = TracerProvider(resource=resource)
        jaeger_exporter = JaegerExporter(
            agent_host_name="jaeger",
            agent_port=6831,
        )
        span_processor = BatchSpanProcessor(jaeger_exporter)
        trace_provider.add_span_processor(span_processor)
        trace.set_tracer_provider(trace_provider)
        self.tracer = trace.get_tracer(__name__)
        
        # Set up metrics
        prometheus_reader = PrometheusMetricReader()
        meter_provider = MeterProvider(resource=resource, metric_readers=[prometheus_reader])
        metrics.set_meter_provider(meter_provider)
        self.meter = metrics.get_meter(__name__)
    
    def _setup_logging(self):
        """Initialize structured logging with trace correlation."""
        # Configure structlog
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
        self.logger = structlog.get_logger()
    
    def _setup_metrics(self):
        """Initialize custom Prometheus metrics."""
        # Request metrics
        self.request_counter = Counter(
            'http_requests_total',
            'Total HTTP requests',
            ['method', 'endpoint', 'status', 'tenant_id', 'priority']
        )
        
        self.request_duration = Histogram(
            'http_request_duration_seconds',
            'HTTP request duration in seconds',
            ['method', 'endpoint', 'tenant_id', 'priority']
        )
        
        # Task metrics
        self.task_counter = Counter(
            'tasks_total',
            'Total tasks processed',
            ['status', 'tenant_id', 'priority']
        )
        
        self.task_duration = Histogram(
            'task_duration_seconds',
            'Task execution duration in seconds',
            ['tenant_id', 'priority', 'status']
        )
        
        # LLM metrics
        self.llm_request_counter = Counter(
            'llm_requests_total',
            'Total LLM requests',
            ['status', 'attempt']
        )
        
        self.llm_request_duration = Histogram(
            'llm_request_duration_seconds',
            'LLM request duration in seconds',
            ['status', 'attempt']
        )
        
        self.llm_token_usage = Counter(
            'llm_tokens_total',
            'Total LLM tokens used',
            ['type']  # prompt or completion
        )
        
        # Tool execution metrics
        self.tool_execution_counter = Counter(
            'tool_executions_total',
            'Total tool executions',
            ['tool_name', 'status']
        )
        
        self.tool_execution_duration = Histogram(
            'tool_execution_duration_seconds',
            'Tool execution duration in seconds',
            ['tool_name']
        )
        
        # System metrics
        self.active_tasks = Gauge(
            'active_tasks',
            'Number of currently active tasks'
        )
        
        self.cache_hits = Counter(
            'cache_hits_total',
            'Total cache hits',
            ['cache_type']
        )
        
        # Circuit breaker metrics
        self.circuit_breaker_state = Counter(
            'circuit_breaker_state_changes_total',
            'Circuit breaker state changes',
            ['state']  # open, closed, half_open, half_open_reject
        )
    
    def instrument_fastapi(self, app):
        """Instrument FastAPI application after it's created."""
        if not self._instrumented:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
                from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
                FastAPIInstrumentor.instrument_app(app)
                HTTPXClientInstrumentor.instrument()
                self._instrumented = True
                self.logger.info("FastAPI instrumentation completed")
            except Exception as e:
                self.logger.error("Failed to instrument FastAPI", error=str(e))
    
    @contextmanager
    def trace_operation(self, operation_name: str, **attributes):
        """Context manager for creating spans with automatic logging."""
        span = self.tracer.start_span(operation_name)
        with trace.use_span(span, end_on_exit=True):
            try:
                # Add attributes to span (only simple types allowed)
                for key, value in attributes.items():
                    if isinstance(value, (str, int, float, bool)):
                        span.set_attribute(key, value)
                    elif isinstance(value, (list, tuple)):
                        # Convert lists to comma-separated strings
                        try:
                            span.set_attribute(key, ",".join(str(v) for v in value))
                        except:
                            span.set_attribute(key, str(value))
                    else:
                        # Convert complex objects to strings
                        try:
                            span.set_attribute(key, str(value))
                        except:
                            # Skip this attribute if it can't be converted
                            continue
                
                # Add trace context to logger
                trace_id = format(span.get_span_context().trace_id, "032x")
                span_id = format(span.get_span_context().span_id, "016x")
                
                # Only include simple attributes in logger binding
                logger_attrs = {
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "operation": operation_name
                }
                
                # Add only simple type attributes to logger
                for key, value in attributes.items():
                    if isinstance(value, (str, int, float, bool)):
                        logger_attrs[key] = value
                    else:
                        # Convert complex objects to strings for logging
                        try:
                            logger_attrs[key] = str(value)
                        except:
                            logger_attrs[key] = "complex_object"
                
                logger = self.logger.bind(**logger_attrs)
                
                logger.info("Operation started")
                
                # Create a safe logger that filters complex objects
                class SafeLogger:
                    def __init__(self, logger):
                        self.logger = logger
                    
                    def _filter_kwargs(self, kwargs):
                        safe_kwargs = {}
                        for k, v in kwargs.items():
                            if isinstance(v, (str, int, float, bool)):
                                safe_kwargs[k] = v
                            else:
                                try:
                                    safe_kwargs[k] = str(v)
                                except:
                                    safe_kwargs[k] = "complex_object"
                        return safe_kwargs
                    
                    def info(self, message, **kwargs):
                        safe_kwargs = self._filter_kwargs(kwargs)
                        self.logger.info(message, **safe_kwargs)
                    
                    def warning(self, message, **kwargs):
                        safe_kwargs = self._filter_kwargs(kwargs)
                        self.logger.warning(message, **safe_kwargs)
                    
                    def error(self, message, **kwargs):
                        safe_kwargs = self._filter_kwargs(kwargs)
                        self.logger.error(message, **safe_kwargs)
                
                yield SafeLogger(logger)
                
            except Exception as e:
                span.record_exception(e)
                logger.error("Operation failed", error=str(e), exc_info=True)
                raise
    
    def get_metrics_response(self) -> Response:
        """Return Prometheus metrics as HTTP response."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Global observability instance
obs = ObservabilityConfig()