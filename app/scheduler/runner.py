"""APScheduler runner that drives recurring sends + IMAP polls.

Wired to the Qt event loop in ``app.main``; jobs themselves stay agnostic.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

log = logging.getLogger(__name__)


class SchedulerRunner:
    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler(daemon=True)

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
            log.info("Scheduler started")

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            log.info("Scheduler shut down")
