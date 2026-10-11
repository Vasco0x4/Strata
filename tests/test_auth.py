import time

import pytest

from strata import auth
from strata.config import Quota
from strata.db import Database
from strata.errors import Conflict, StrataError


def test_token_roundtrip_and_storage():
    db = Database(":memory:")
    token = auth.create_agent(db, "alpha", Quota())
    assert token.startswith("strata_") and len(token) > 40
    row = db.one("SELECT token_hash FROM agents WHERE name = 'alpha'")
    assert token not in row["token_hash"]  # only the hash is stored
    caller = auth.authenticate(db, token)
    assert caller.name == "alpha" and not caller.is_admin


def test_bad_tokens_rejected():
    db = Database(":memory:")
    token = auth.create_agent(db, "alpha", Quota())
    for bad in (None, "", "strata_nope", token[:-1], token.removeprefix("strata_")):
        assert auth.authenticate(db, bad) is None


def test_revoke_and_expiry():
    db = Database(":memory:")
    token = auth.create_agent(db, "alpha", Quota())
    auth.revoke_agent(db, "alpha")
    assert auth.authenticate(db, token) is None

    token = auth.create_agent(db, "beta", Quota(), expires_in_days=1)
    assert auth.authenticate(db, token) is not None
    db.execute("UPDATE agents SET expires_at = ? WHERE name = 'beta'", (time.time() - 1,))
    assert auth.authenticate(db, token) is None


def test_names():
    db = Database(":memory:")
    auth.create_agent(db, "alpha", Quota())
    with pytest.raises(Conflict):
        auth.create_agent(db, "alpha", Quota())
    for bad in ("", "Alpha", "a b", "x" * 40, "../x", "alpha2\n"):
        with pytest.raises(StrataError):
            auth.create_agent(db, bad, Quota())
