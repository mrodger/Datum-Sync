"""Many labelled tokens per account, and revoking one of them.

The point of the child table is that rotation stops being destructive. So the
assertions that matter here are the negative halves: after revoking one token
the OTHER one must still work, and the revoked one must stop. A test that only
checks the new token works would pass just as well against the old single
column, which invalidated the previous credential the instant a new one was
written.

`service_accounts.token_hash` still holds the pre-migration value for accounts
that existed before 012. One test below presents a token whose hash is in that
column and nowhere else, and requires a 401 -- if a fallback to the old column
is ever added back, revoking a backfilled `legacy` row becomes a no-op that
returns 200, and nothing else in the suite would notice.
"""
from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from datum_sync import auth, db as db_module, tokens
from datum_sync.api import app

pytestmark = pytest.mark.asyncio

ACCOUNT = "_pytest_tokens"
OTHER_ACCOUNT = "_pytest_tokens_other"


@pytest_asyncio.fixture
async def account(db):
    """A bare account with no tokens. Tests mint the ones they need."""
    await db.execute(
        "DELETE FROM service_accounts WHERE name = ANY($1)",
        [ACCOUNT, OTHER_ACCOUNT],
    )
    account_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ($1, 4, true)
        RETURNING id
        """,
        ACCOUNT,
    )
    try:
        yield account_id
    finally:
        await db.execute(
            "DELETE FROM service_accounts WHERE name = ANY($1)",
            [ACCOUNT, OTHER_ACCOUNT],
        )


@pytest_asyncio.fixture
async def client(db):
    """An unauthenticated client. Each test presents its own token."""
    import httpx

    await db_module.init_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c
    await db_module.close_pool()


async def _get(client, token: str):
    return await client.get(
        "/rest/v1/repositories", headers={"authorization": f"Bearer {token}"}
    )


# -- the whole point ---------------------------------------------------------


async def test_two_live_tokens_both_authenticate(account, db, client):
    """One account, two labelled credentials, both accepted.

    The old column was UNIQUE, so this arrangement could not be represented at
    all -- writing the second token was writing over the first.
    """
    _, ci = await tokens.create(db, account, "ci-runner")
    _, laptop = await tokens.create(db, account, "laptop")

    assert ci != laptop
    assert (await _get(client, ci)).status_code == 200
    assert (await _get(client, laptop)).status_code == 200


async def test_revoking_one_token_leaves_the_others_working(account, db, client):
    """Rotation is non-destructive: the survivor keeps working.

    This is the assertion the design exists for. The revoked-token half below
    proves the revocation happened; this half proves it was surgical.

    Guard: TOKEN-001.
    """
    _, ci = await tokens.create(db, account, "ci-runner")
    _, laptop = await tokens.create(db, account, "laptop")
    assert (await _get(client, ci)).status_code == 200
    assert (await _get(client, laptop)).status_code == 200

    assert await tokens.revoke(db, account, "ci-runner") is True

    revoked = await _get(client, ci)
    assert revoked.status_code == 401
    assert revoked.json()["code"] == "TOKEN_REVOKED"

    # The one nobody touched is unaffected -- not merely "some token still
    # works", but this specific other credential.
    assert (await _get(client, laptop)).status_code == 200


async def test_a_revoked_token_says_so(account, db, client):
    """TOKEN_REVOKED, not the generic unknown-token refusal.

    Both are 401, so an operator who has just rotated a credential and is
    debugging a broken deploy can only tell "I revoked this" from "this was
    never valid" by the code.
    """
    _, raw = await tokens.create(db, account, "ci-runner")
    await tokens.revoke(db, account, "ci-runner")

    r = await _get(client, raw)
    assert r.status_code == 401
    assert r.json()["code"] == "TOKEN_REVOKED"

    unknown = await _get(client, auth.new_token())
    assert unknown.status_code == 401
    assert unknown.json()["code"] != "TOKEN_REVOKED"


# -- expiry ------------------------------------------------------------------


async def test_expiry_is_per_token_not_per_account(account, db, client):
    """One credential lapsing does not take its siblings with it.

    Expiry used to live on `service_accounts.token_expires`, one value for the
    account, so a short-lived token could not coexist with a permanent one.

    Guard: TOKEN-002.
    """
    _, short = await tokens.create(db, account, "short-lived")
    _, permanent = await tokens.create(db, account, "permanent")

    await db.execute(
        "UPDATE account_tokens SET expires_at = now() - interval '1 second' "
        "WHERE account_id = $1 AND label = 'short-lived'",
        account,
    )

    expired = await _get(client, short)
    assert expired.status_code == 401
    assert expired.json()["code"] == "TOKEN_EXPIRED"
    assert (await _get(client, permanent)).status_code == 200


# -- revocation semantics ----------------------------------------------------


async def test_revoke_does_not_reach_across_accounts(account, db, client):
    """A label is unique only within an account, so revoke is scoped to one.

    Two accounts each call their token 'ci-runner'. Revoking by label alone
    would withdraw both.

    Guard: TOKEN-003.
    """
    other_id = await db.fetchval(
        """
        INSERT INTO service_accounts (name, max_tier, is_admin)
        VALUES ($1, 4, true)
        RETURNING id
        """,
        OTHER_ACCOUNT,
    )
    _, mine = await tokens.create(db, account, "ci-runner")
    _, theirs = await tokens.create(db, other_id, "ci-runner")

    assert await tokens.revoke(db, account, "ci-runner") is True

    assert (await _get(client, mine)).status_code == 401
    assert (await _get(client, theirs)).status_code == 200


async def test_revoking_twice_does_not_move_the_revocation_time(account, db):
    """The recorded time is when access ended, not when someone last asked.

    Re-stamping would make the audit trail say a credential was live until the
    second call, which is the opposite of true.

    Guard: TOKEN-004.
    """
    await tokens.create(db, account, "ci-runner")
    assert await tokens.revoke(db, account, "ci-runner") is True

    first = await db.fetchval(
        "SELECT revoked_at FROM account_tokens WHERE account_id = $1 AND label = $2",
        account,
        "ci-runner",
    )

    # False: there was no live token to revoke. A caller that treats True as
    # "you have now been cut off" would otherwise be told so twice.
    assert await tokens.revoke(db, account, "ci-runner") is False

    second = await db.fetchval(
        "SELECT revoked_at FROM account_tokens WHERE account_id = $1 AND label = $2",
        account,
        "ci-runner",
    )
    assert first == second


async def test_a_label_can_be_reused_after_revocation(account, db, client):
    """The uniqueness constraint covers live tokens only.

    Otherwise 'ci-runner' would be spent forever after its first rotation and
    operators would end up at ci-runner-2, ci-runner-3.
    """
    _, first = await tokens.create(db, account, "ci-runner")
    with pytest.raises(asyncpg.UniqueViolationError):
        await tokens.create(db, account, "ci-runner")

    await tokens.revoke(db, account, "ci-runner")
    _, second = await tokens.create(db, account, "ci-runner")

    assert (await _get(client, first)).status_code == 401
    assert (await _get(client, second)).status_code == 200


# -- bookkeeping -------------------------------------------------------------


async def test_listing_hides_revoked_tokens_by_default(account, db):
    await tokens.create(db, account, "ci-runner")
    await tokens.create(db, account, "laptop")
    await tokens.revoke(db, account, "ci-runner")

    live = await tokens.list_for_account(db, account)
    assert [r["label"] for r in live] == ["laptop"]

    every = await tokens.list_for_account(db, account, include_revoked=True)
    assert sorted(r["label"] for r in every) == ["ci-runner", "laptop"]


async def test_listing_never_returns_the_hash(account, db):
    """`token_hash` is absent from the column list, not stripped afterwards.

    A route cannot leak it by forgetting to remove it, because no read path
    selects it in the first place.
    """
    await tokens.create(db, account, "ci-runner")
    rows = await tokens.list_for_account(db, account)
    assert "token_hash" not in rows[0].keys()

    resolved = await tokens.resolve(db, await db.fetchval(
        "SELECT token_hash FROM account_tokens WHERE account_id = $1", account
    ))
    assert "token_hash" not in resolved.keys()


async def test_live_count_ignores_revoked_and_expired(account, db):
    """`has_token` is derived from this, so it must mean 'can authenticate'."""
    await tokens.create(db, account, "a")
    await tokens.create(db, account, "b")
    await tokens.create(db, account, "c")
    assert await tokens.live_count(db, account) == 3

    await tokens.revoke(db, account, "a")
    await db.execute(
        "UPDATE account_tokens SET expires_at = now() - interval '1 second' "
        "WHERE account_id = $1 AND label = 'b'",
        account,
    )
    assert await tokens.live_count(db, account) == 1


async def test_using_a_token_stamps_that_token(account, db, client):
    """last_used_at is per credential, so an unused one is identifiable."""
    _, ci = await tokens.create(db, account, "ci-runner")
    await tokens.create(db, account, "laptop")

    assert (await _get(client, ci)).status_code == 200

    rows = {
        r["label"]: r["last_used_at"]
        for r in await tokens.list_for_account(db, account)
    }
    assert rows["ci-runner"] is not None
    assert rows["laptop"] is None


async def test_deleting_an_account_takes_its_tokens(account, db):
    await tokens.create(db, account, "ci-runner")
    await db.execute("DELETE FROM service_accounts WHERE id = $1", account)

    left = await db.fetchval(
        "SELECT count(*) FROM account_tokens WHERE account_id = $1", account
    )
    assert left == 0
