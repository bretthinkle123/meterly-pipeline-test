"""Validation contract for the dashboard BFF routes.

`UsageSeriesQueryParams` is the one untrusted input surface this feature
introduces (`GET /dashboard/api/usage-series`). Every field is bounded by an
anchored allowlist pattern reused from `src/api/schemas/events.py` *and*
membership in the deployment's configured allowlist, so the same-origin BFF
can never be used to enumerate arbitrary customers (I-D3) or request a
granularity the hourly rollup can't serve correctly (`month` is excluded per
the human decision on Q1 — ship hour/day live, disable month in the UI).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from src.api.schemas.events import CustomerId, Metric
from src.config.settings import get_settings

# `month` is deliberately absent: the hourly rollup cannot serve it correctly
# within the 90-day lookback bound (plan §"Window granularity", Q1). The
# segmented control still *renders* a month segment for visual fidelity, but
# it is disabled client-side and can never reach this schema.
Granularity = Literal["hour", "day"]


class UsageSeriesQueryParams(BaseModel):
    """The three query parameters of `GET /dashboard/api/usage-series`."""

    model_config = ConfigDict(extra="forbid")

    customer_id: CustomerId
    metric: Metric
    granularity: Granularity

    @field_validator("customer_id")
    @classmethod
    def _customer_id_in_allowlist(cls, value: str) -> str:
        """Bound `customer_id` to the deployment's configured customer set —
        the boundary allowlist that stops the unauthenticated BFF from being
        used to enumerate arbitrary tenants' customer ids (I-D3)."""
        if value not in get_settings().dashboard_customers:
            raise ValueError("customer_id is not in the configured allowlist")
        return value

    @field_validator("metric")
    @classmethod
    def _metric_in_allowlist(cls, value: str) -> str:
        """Bound `metric` to the deployment's configured metric set."""
        if value not in get_settings().dashboard_metrics:
            raise ValueError("metric is not in the configured allowlist")
        return value


class UsageSeriesRow(BaseModel):
    """One row of the "Recent windows" table (`CMP-7`) — already formatted
    for display so `dashboard.js` only ever writes text, never computes."""

    model_config = ConfigDict(extra="forbid")

    window_start: str
    metric: str
    quantity: str
    delta_text: str
    delta_direction: Literal["up", "down", "neutral"]


class CurrentUsage(BaseModel):
    """The `CMP-5` stat card's populated-state fields."""

    model_config = ConfigDict(extra="forbid")

    window_start: str
    quantity: str
    metric: str
    window_label: str
    delta_text: str
    delta_direction: Literal["up", "down", "neutral"]


class UsageSeriesResponse(BaseModel):
    """The minimal response field set for `GET /dashboard/api/usage-series`
    (ASVS 15.3.1) — a `state` discriminator plus exactly the fields the
    populated/empty render states need."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["populated", "empty"]
    current: CurrentUsage
    rows: list[UsageSeriesRow]


class ConfigResponse(BaseModel):
    """`GET /dashboard/api/config` — the single server-side source of truth
    shared by the page's `CMP-3` dropdown option lists, the `CMP-2` env
    badge, and this file's allowlist validators (so the two never drift)."""

    model_config = ConfigDict(extra="forbid")

    customers: list[str]
    metrics: list[str]
    granularities: list[str]
    environment: str
