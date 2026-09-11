
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass

import httpx
import jwt

BASE_URL = os.getenv("WALLET_BASE_URL", "http://127.0.0.1:8000")
JWT_SECRET = os.getenv("JWT_SECRET", "local-development-only-secret-change-me")
TEST_SEED_KEY = os.getenv("TEST_SEED_KEY", "local-test-seed-key-change-me")


@dataclass(frozen=True)
class UserWallet:
    user_id: uuid.UUID
    wallet_id: str


def token_headers(user_id: uuid.UUID, correlation_id: str | None = None) -> dict[str, str]:
    token = jwt.encode({"sub": str(user_id)}, JWT_SECRET, algorithm="HS256")
    headers = {"Authorization": f"Bearer {token}"}
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    return headers


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


async def create_wallet(client: httpx.AsyncClient, user_id: uuid.UUID) -> UserWallet:
    response = await client.post("/wallets", headers=token_headers(user_id))
    response.raise_for_status()
    return UserWallet(user_id=user_id, wallet_id=response.json()["id"])


async def concurrent_wallet_creation(client: httpx.AsyncClient) -> None:
    user_id = uuid.uuid4()
    responses = await asyncio.gather(
        *(client.post("/wallets", headers=token_headers(user_id)) for _ in range(50))
    )
    wallet_ids = {response.json()["id"] for response in responses if response.status_code == 200}
    assert len(wallet_ids) == 1, f"expected one wallet ID, received {wallet_ids}"
    assert len(responses) == 50 and all(response.status_code == 200 for response in responses)
    report("concurrent_wallet_creation_passed", requests=50, wallet_id=wallet_ids.pop())


async def fund_wallets(
    client: httpx.AsyncClient, wallets: list[UserWallet], amount_paise: int
) -> None:
    responses = await asyncio.gather(
        *(
            client.post(
                f"/internal/test/wallets/{wallet.wallet_id}/fund",
                json={"amount_paise": amount_paise},
                headers={"X-Test-Seed-Key": TEST_SEED_KEY},
            )
            for wallet in wallets
        )
    )
    assert all(response.status_code == 200 for response in responses)


async def balances_for(client: httpx.AsyncClient, wallets: list[UserWallet]) -> list[int]:
    responses = await asyncio.gather(
        *(client.get(f"/wallets/{wallet.wallet_id}", headers=token_headers(wallet.user_id)) for wallet in wallets)
    )
    assert all(response.status_code == 200 for response in responses)
    return [response.json()["balance_paise"] for response in responses]


async def idempotency_storm(client: httpx.AsyncClient) -> None:
    sender, recipient = await asyncio.gather(
        create_wallet(client, uuid.uuid4()), create_wallet(client, uuid.uuid4())
    )
    await fund_wallets(client, [sender], 10_000)
    key = f"retry-storm-{uuid.uuid4()}"
    payload = {
        "from": sender.wallet_id,
        "to": recipient.wallet_id,
        "amount_paise": 750,
        "idempotency_key": key,
    }
    responses = await asyncio.gather(
        *(
            client.post(
                "/transfers",
                json=payload,
                headers=token_headers(sender.user_id, f"idempotency-{index}"),
            )
            for index in range(30)
        )
    )
    bodies = [response.json() for response in responses]
    assert all(response.status_code == 200 for response in responses)
    assert {body["id"] for body in bodies} == {bodies[0]["id"]}
    assert {body["status"] for body in bodies} == {"completed"}
    balances = await balances_for(client, [sender, recipient])
    assert sum(balances) == 10_000
    report(
        "idempotency_storm_passed",
        requests=30,
        transfer_id=bodies[0]["id"],
        sender_balance=balances[0],
        recipient_balance=balances[1],
    )


async def conservation_under_contention(
    client: httpx.AsyncClient,
) -> None:
    wallets = await asyncio.gather(*(create_wallet(client, uuid.uuid4()) for _ in range(6)))
    await fund_wallets(client, wallets, 5_000)
    total_before = sum(await balances_for(client, wallets))
    semaphore = asyncio.Semaphore(12)

    async def submit(index: int) -> int:
        source = wallets[index % len(wallets)]
        destination = wallets[(index + 1) % len(wallets)]
        payload = {
            "from": source.wallet_id,
            "to": destination.wallet_id,
            "amount_paise": 125 if index % 5 else 20_000,
            "idempotency_key": f"contention-{uuid.uuid4()}",
        }
        async with semaphore:
            response = await client.post(
                "/transfers", json=payload, headers=token_headers(source.user_id)
            )
        assert response.status_code == 200, response.text
        return 1 if response.json()["status"] == "completed" else 0

    completed = sum(await asyncio.gather(*(submit(index) for index in range(300))))
    balances = await balances_for(client, wallets)
    total_after = sum(balances)
    negative_wallets = sum(balance < 0 for balance in balances)
    assert total_after == total_before
    assert negative_wallets == 0
    report(
        "conservation_contention_passed",
        requests=300,
        completed=completed,
        declined=300 - completed,
        total_before=total_before,
        total_after=total_after,
        negative_wallets=negative_wallets,
    )


async def main() -> None:
    started_at = time.perf_counter()
    report("burst_started", base_url=BASE_URL)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        await concurrent_wallet_creation(client)
        await idempotency_storm(client)
        await conservation_under_contention(client)
    report("burst_passed", duration_ms=round((time.perf_counter() - started_at) * 1000, 2))


if __name__ == "__main__":
    asyncio.run(main())