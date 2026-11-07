"""Content-free model usage accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from threading import Lock
from typing import Any

from .contracts import ModelCallResult
from .errors import ProviderError


@dataclass(frozen=True)
class UsageRecord:
    recorded_at: str
    request_id: str
    task_type: str
    provider_id: str
    requested_model: str
    returned_model: str | None
    provider_request_id: str | None
    status: str
    error_code: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_cny: float
    latency_ms: float


class UsageLedger:
    """Thread-safe in-memory ledger that never stores prompt or response content."""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []
        self._lock = Lock()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def record_success(self, result: ModelCallResult) -> None:
        usage = result.usage
        self.add(
            UsageRecord(
                recorded_at=self._now(),
                request_id=result.request_id,
                task_type=result.task_type,
                provider_id=result.provider_id,
                requested_model=result.requested_model,
                returned_model=result.returned_model,
                provider_request_id=result.provider_request_id,
                status="success",
                error_code=None,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                estimated_cost_cny=result.estimated_cost_cny,
                latency_ms=result.latency_ms,
            )
        )

    def record_failure(
        self,
        *,
        request_id: str,
        task_type: str,
        provider_id: str,
        requested_model: str,
        error: ProviderError,
        latency_ms: float,
    ) -> None:
        self.add(
            UsageRecord(
                recorded_at=self._now(),
                request_id=request_id,
                task_type=task_type,
                provider_id=provider_id,
                requested_model=requested_model,
                returned_model=None,
                provider_request_id=error.provider_request_id,
                status="failed",
                error_code=error.code.value,
                input_tokens=error.input_tokens,
                output_tokens=error.output_tokens,
                total_tokens=error.total_tokens,
                estimated_cost_cny=error.estimated_cost_cny,
                latency_ms=latency_ms,
            )
        )

    def add(self, record: UsageRecord) -> None:
        with self._lock:
            self._records.append(record)

    def records(self) -> tuple[UsageRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def cursor(self) -> int:
        """Return an opaque record offset for later delta summaries."""

        with self._lock:
            return len(self._records)

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    def summary(
        self,
        *,
        request_id: str | None = None,
        start_index: int = 0,
    ) -> dict[str, Any]:
        if (
            not isinstance(start_index, int)
            or isinstance(start_index, bool)
            or start_index < 0
        ):
            raise ValueError("start_index must be a non-negative integer")
        records = tuple(
            record
            for record in self.records()[start_index:]
            if request_id is None or record.request_id == request_id
        )
        latencies = [record.latency_ms for record in records]
        success_count = sum(record.status == "success" for record in records)
        error_counts: dict[str, int] = {}
        for record in records:
            if record.error_code:
                error_counts[record.error_code] = error_counts.get(record.error_code, 0) + 1
        return {
            "calls": len(records),
            "attempts": len(records),
            "successes": success_count,
            "failures": len(records) - success_count,
            "input_tokens": sum(record.input_tokens for record in records),
            "output_tokens": sum(record.output_tokens for record in records),
            "total_tokens": sum(record.total_tokens for record in records),
            "estimated_cost_cny": round(
                sum(record.estimated_cost_cny for record in records), 8
            ),
            "latency_total_ms": round(
                sum(record.latency_ms for record in records), 3
            ),
            "latency_p50_ms": self._percentile(latencies, 0.50),
            "latency_p95_ms": self._percentile(latencies, 0.95),
            "error_counts": error_counts,
        }


__all__ = ["UsageLedger", "UsageRecord"]
