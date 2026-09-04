"""Run the edge AI surveillance pipeline."""

from __future__ import annotations

import asyncio
import logging

from config.device_settings import DeviceSettings
from core.runtime.pipeline import Pipeline


async def async_main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = DeviceSettings.from_environment()
    pipeline = await Pipeline.create(settings)
    try:
        await pipeline.run()
    finally:
        await pipeline.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
