"""DecisionTracker — tracks execution of previous CEO cycle decisions."""
from __future__ import annotations

from typing import Any, Dict, List


class DecisionTracker:
    """Tracks and summarizes execution of CEO decisions from previous cycles."""

    def get_execution_summary(self, previous_tasks: List[Dict]) -> Dict[str, Any]:
        """Summarize execution of previous decisions.

        Args:
            previous_tasks: List of task dicts with keys 'title'/'action', 'status'.

        Returns:
            Dict with execution stats and lists of done/pending task titles.
        """
        done = [t for t in previous_tasks if t.get("status") == "done"]
        in_progress = [t for t in previous_tasks if t.get("status") == "in_progress"]
        pending = [t for t in previous_tasks if t.get("status") == "pending"]
        total = len(previous_tasks)

        def _title(t: Dict) -> str:
            return t.get("title") or t.get("action", "")

        return {
            "done_count": len(done),
            "in_progress_count": len(in_progress),
            "pending_count": len(pending),
            "execution_rate": len(done) / max(total, 1),
            "done_titles": [_title(t) for t in done],
            "next_focus": _title(pending[0]) if pending else "none",
        }

    def generate_accountability_report(self, summary: Dict) -> str:
        """Generate an accountability section for CEO report.

        Args:
            summary: Dict returned by get_execution_summary().

        Returns:
            Formatted multi-line string with execution stats.
        """
        rate_pct = int(summary.get("execution_rate", 0) * 100)
        return (
            f"📊 Выполнение прошлых решений: {rate_pct}%\n"
            f"✅ Выполнено: {summary.get('done_count', 0)}\n"
            f"🔄 В процессе: {summary.get('in_progress_count', 0)}\n"
            f"⏳ Ожидает: {summary.get('pending_count', 0)}\n"
        )
