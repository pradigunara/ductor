"""Background observer for periodic Command Code model cache refresh."""

from __future__ import annotations

from ductor_bot.cli.commandcode_cache import CommandCodeModelCache
from ductor_bot.cli.model_cache import BaseModelCacheObserver


class CommandCodeCacheObserver(BaseModelCacheObserver[CommandCodeModelCache]):
    """Refreshes the Command Code model cache periodically.

    Loads the initial cache at startup and refreshes every 60 minutes. Pass
    ``on_refresh`` (see base) to receive the model tuple after each load.
    """

    def _provider_name(self) -> str:
        return "Command Code"

    async def _load_cache(self, *, initial: bool) -> CommandCodeModelCache:
        return await CommandCodeModelCache.load_or_refresh(self._cache_path, force_refresh=initial)
