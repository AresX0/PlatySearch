"""Robots.txt parser and rate-limiter for polite crawling."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import aiohttp


class RobotsChecker:
    """Caches and evaluates robots.txt rules per domain."""

    def __init__(self, user_agent: str) -> None:
        self._user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}

    async def is_allowed(self, url: str, session: aiohttp.ClientSession) -> bool:
        """Check whether *url* may be fetched according to robots.txt."""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if origin not in self._parsers:
            await self._fetch_robots(origin, session)

        parser = self._parsers.get(origin)
        if parser is None:
            return True  # No robots.txt → allowed.
        return parser.can_fetch(self._user_agent, url)

    async def wait_for_rate_limit(self, domain: str, delay: float) -> None:
        """Sleep if we're making requests to *domain* too fast."""
        now = time.monotonic()
        last = self._last_request.get(domain, 0.0)
        wait = delay - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request[domain] = time.monotonic()

    # ── internal ──────────────────────────────────────────────────

    async def _fetch_robots(
        self, origin: str, session: aiohttp.ClientSession
    ) -> None:
        robots_url = f"{origin}/robots.txt"
        try:
            async with session.get(robots_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    parser = RobotFileParser()
                    parser.parse(text.splitlines())
                    self._parsers[origin] = parser
                else:
                    self._parsers[origin] = None
        except Exception:
            self._parsers[origin] = None
