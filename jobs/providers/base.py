"""Swappable data-provider layer.

Every data need (prices, fundamentals, analyst, ...) is a *capability*. Each
capability has an ordered list of providers in config/providers.yaml. Callers
ask the registry for a capability; the registry tries providers in order and
falls through on failure, recording which one answered. A dead source never
breaks a job — it just moves to the backup and logs a health event.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """Raised by a provider when it cannot serve a request (down, blocked, rate-limited, empty)."""


class NotSupported(ProviderError):
    """Provider does not offer this capability (or only on a paid tier)."""


@dataclass
class ProviderResult:
    capability: str
    provider: str
    data: Any
    fetched_at: float = field(default_factory=time.time)


class Provider:
    """Base class. Subclasses set `name` and implement capability methods
    named after the capability, e.g. `def daily_prices(self, ticker, start, end)`."""

    name: str = "base"
    #: requests per second ceiling; the registry sleeps to respect it
    max_rps: float = 1.0

    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        self._last_call = 0.0

    def throttle(self) -> None:
        wait = (1.0 / self.max_rps) - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def supports(self, capability: str) -> bool:
        return callable(getattr(self, capability, None))


class Registry:
    """Ordered fallback across providers per capability."""

    def __init__(self, order: dict[str, list[str]], providers: dict[str, Provider],
                 on_failure: Callable[[str, str, Exception], None] | None = None) -> None:
        self.order = order
        self.providers = providers
        self.on_failure = on_failure or (lambda cap, prov, exc: log.warning("%s via %s failed: %s", cap, prov, exc))

    def call(self, capability: str, *args: Any, **kwargs: Any) -> ProviderResult:
        errors: list[str] = []
        for pname in self.order.get(capability, []):
            prov = self.providers.get(pname)
            if prov is None or not prov.supports(capability):
                continue
            try:
                prov.throttle()
                data = getattr(prov, capability)(*args, **kwargs)
                if data is None:
                    raise ProviderError("empty response")
                return ProviderResult(capability, pname, data)
            except Exception as exc:  # noqa: BLE001 - fall through to backup on any failure
                self.on_failure(capability, pname, exc)
                errors.append(f"{pname}: {exc}")
        raise ProviderError(f"all providers failed for {capability}: " + "; ".join(errors))
