"""Native metrics: ingest, storage, retention and duration-based alerting.

HomePilot collects its own metrics over the agent channel it already owns
(ADR-004 S5). This package owns everything downstream of the agent's ``metrics``
frame; the frame itself is handled in ``agent_hub/server.py``.
"""

from .alerts import AlertEvaluator
from .repository import COMPARISON_FUNCS, MAX_SERIES_POINTS, MetricsRepository
from .retention import MetricsPruner

__all__ = [
    "COMPARISON_FUNCS",
    "MAX_SERIES_POINTS",
    "AlertEvaluator",
    "MetricsPruner",
    "MetricsRepository",
]
