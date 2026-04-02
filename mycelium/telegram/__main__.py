"""Entry point: python -m mycelium.telegram"""

import asyncio

from mycelium.telegram.bot import run_bot

if __name__ == "__main__":
    asyncio.run(run_bot())
