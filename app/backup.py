"""Backup/restore of the agent's memory (manual, via environment).

DEPRECATED: use app.notebook (GitHub-backed) instead — kept so old
imports don't break.
"""

from .notebook import push_snapshot, restore_if_available  # noqa: F401
