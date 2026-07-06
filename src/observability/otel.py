"""OpenTelemetry wiring — traces/metrics exported to the ADOT collector
sidecar, which forwards to X-Ray (traces) and CloudWatch (metrics).

Configured once from the lifespan startup hook (`src/main.py`), never at
import time, so `import src.main` stays dependency-free for the smoke check.
"""

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from src.config.settings import Settings


def configure_otel(app: FastAPI, settings: Settings) -> None:
    """Set up the OTel tracer provider and instrument the FastAPI app.

    A no-op (in-memory only, no exporter) when `otel_exporter_otlp_endpoint`
    is unset, so local/dev/smoke runs never require a reachable collector.
    """
    resource = Resource.create({SERVICE_NAME: settings.service_name})
    provider = TracerProvider(resource=resource)

    if settings.otel_exporter_otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
