import asyncio

from src.database import engine
from src.transfers import models as transfer_models
from src.wallets.models import Base


async def main() -> None:
    _ = transfer_models
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


if __name__ == "__main__":
    asyncio.run(main())