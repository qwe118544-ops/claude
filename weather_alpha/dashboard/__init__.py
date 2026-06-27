"""Read-only trading dashboard.

The dashboard renders the engine's published snapshot. It contains NO trading
logic — discovery, signalling, sizing and execution all live in the backend
engine; this is purely a view. Run with:

    python -m weather_alpha.dashboard --port 8787 --markets 60 --interval 0.5
"""

from .server import DashboardServer, run

__all__ = ["DashboardServer", "run"]
