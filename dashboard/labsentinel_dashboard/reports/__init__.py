"""Per-session reports: one data model, rendered as PDF or Excel."""

from labsentinel_dashboard.reports.model import (
    ReportError,
    SessionReport,
    build_session_report,
)

__all__ = ["ReportError", "SessionReport", "build_session_report"]
