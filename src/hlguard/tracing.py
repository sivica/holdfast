from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from hlguard.redact import redact

_PATH: Path | None = None
_READY = False


class JsonlExporter(SpanExporter):
    def export(self, spans) -> SpanExportResult:
        path = _PATH
        if path is None:
            return SpanExportResult.SUCCESS
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            from hlguard.store import locked

            with locked(path, "r+") as file:
                raw = file.read()
                if raw and not raw.endswith("\n"):
                    file.write("\n")
                for span in spans:
                    file.write(json.dumps(_span_dict(span), sort_keys=True) + "\n")
        except Exception:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def configure_tracing(path: Path) -> None:
    global _PATH, _READY
    _PATH = path
    path.parent.mkdir(parents=True, exist_ok=True)
    if _READY:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "holdfast"}))
    provider.add_span_processor(SimpleSpanProcessor(JsonlExporter()))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _READY = True


def tracer():
    return trace.get_tracer("holdfast")


def _span_dict(span) -> dict:
    context = span.context
    parent = span.parent
    attributes = {key: _jsonable(value) for key, value in (span.attributes or {}).items()}
    return {
        "name": span.name,
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
        "parent": None if parent is None else format(parent.span_id, "016x"),
        "status": span.status.status_code.name,
        "attributes": attributes,
    }


def _jsonable(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return value
    if isinstance(value, Sequence):
        return [_jsonable(item) for item in value]
    return redact(str(value))
