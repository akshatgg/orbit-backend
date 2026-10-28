"""Page fetching with one polite retry, and a client that spaces a platform's requests 1 s
apart (`fetchHtml` and `politeClient` in the TypeScript `connectors.ts`)."""

from . import fetch as http


async def fetch_html(url: str) -> str:
    """One polite retry for a transient failure; a 403 is a block and is not retried."""
    try:
        return await http.fetch_feed_text(url, accept="text/html")
    except http.FetchHttpError as exc:
        if exc.status == 403:
            raise
    except Exception:  # noqa: BLE001 - any other failure gets one retry
        pass
    await http.sleep(1500)
    return await http.fetch_feed_text(url, accept="text/html")


class PoliteClient:
    """Requests to one platform, 1 s apart. Any failure propagates and fails the run: a partial
    list would make the sync flag every event it didn't see as missing."""

    def __init__(self) -> None:
        self.sent = 0

    async def _pause(self) -> None:
        if self.sent > 0:
            await http.sleep(1000)
        self.sent += 1

    async def json(self, url: str) -> str:
        await self._pause()
        return await http.fetch_feed_text(url, accept="application/json")

    async def html(self, url: str) -> str:
        await self._pause()
        return await fetch_html(url)
