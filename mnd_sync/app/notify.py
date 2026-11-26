"""Home Assistant notifications through the Supervisor API. Failures are logged, never raised."""

import logging
import re

import requests

log = logging.getLogger("mnd_sync")


def valid_service(name: str) -> bool:
    return re.fullmatch(r"[a-z0-9_]+", name) is not None


_BASE = "http://supervisor/core/api/services/"


class Notifier:
    def __init__(self, token, service):
        self.token, self.service = token, service

    def send(self, title, message) -> None:
        if not self.token:
            log.warning("notification not sent: no Supervisor token (SUPERVISOR_TOKEN is not set)")
            return
        service = self.service() if callable(self.service) else self.service  # callable: read the setting live
        if service and not valid_service(service):
            log.warning("notify_service is not a valid service name; sending the persistent notification only")
            service = None
        targets = ["persistent_notification/create"] + ([f"notify/{service}"] if service else [])
        for target in targets:
            try:
                requests.post(_BASE + target, json={"title": title, "message": message},
                              headers={"Authorization": f"Bearer {self.token}"}, timeout=10).raise_for_status()
                log.info("notification sent via %s", target)
            except Exception as e:  # noqa: BLE001  # type only: text could carry the URL or token
                log.warning("notification via %s failed (%s)", target, type(e).__name__)
