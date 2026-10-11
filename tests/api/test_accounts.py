"""Accounts: an admin creates them; passwords are hashed with scrypt."""

from __future__ import annotations

import hmac

import pytest
import sqlalchemy as sa

from labelflow.api import passwords
from labelflow.store import connect


def test_an_admin_creates_an_account(admin, client, expect, new_password):
    secret = new_password()
    created = expect(
        admin.post(
            "/accounts",
            {"username": "ana", "password": secret, "display_name": "Ana Lopez"},
        ),
        201,
    )
    assert created == {
        "user_id": "ana",
        "display_name": "Ana Lopez",
        "is_admin": False,
        "active": True,
        "created_by": "root",
        "created_at": "2026-10-10T09:00:00Z",
    }
    assert "password" not in str(created)
    login = client.post("/auth/login", json={"username": "ana", "password": secret})
    assert login.status_code == 200
    assert login.json()["user"]["display_name"] == "Ana Lopez"


def test_the_display_name_defaults_to_the_username(admin, expect, new_password):
    created = expect(admin.post("/accounts", {"username": "ben", "password": new_password()}), 201)
    assert created["display_name"] == "ben"


def test_an_admin_can_create_another_admin(admin, client, expect, new_password):
    secret = new_password()
    expect(
        admin.post("/accounts", {"username": "boss", "password": secret, "is_admin": True}),
        201,
    )
    token = client.post("/auth/login", json={"username": "boss", "password": secret})
    assert token.json()["user"]["is_admin"] is True


def test_a_duplicate_username_is_a_conflict(admin, ana, new_password):
    response = admin.post("/accounts", {"username": "ana", "password": new_password()})
    assert response.status_code == 409
    assert response.json()["detail"] == "An account named 'ana' already exists."


@pytest.mark.parametrize("username", ["Ana", "a b", "-ana", "", "x" * 65, "ana@example.org"])
def test_a_bad_username_is_refused(admin, username, new_password):
    response = admin.post("/accounts", {"username": username, "password": new_password()})
    assert response.status_code == 422, username
    assert "username" in response.json()["detail"].lower()


def test_a_short_password_is_refused(admin):
    response = admin.post("/accounts", {"username": "cy", "password": "x" * 5})
    assert response.status_code == 422
    assert response.json()["detail"] == "The password must be 8 to 256 characters long."


def test_the_accounts_are_listed(admin, ana, ben, expect):
    listed = expect(admin.get("/accounts"))
    assert [a["user_id"] for a in listed] == ["ana", "ben", "root"]
    assert [a["is_admin"] for a in listed] == [False, False, True]
    assert all(a["active"] for a in listed)


def test_deactivating_an_account(admin, ana, expect):
    result = expect(admin.post("/accounts/ana/deactivate"))
    assert result["user_id"] == "ana"
    assert result["active"] is False
    listed = {a["user_id"]: a for a in expect(admin.get("/accounts"))}
    assert listed["ana"]["active"] is False


def test_an_admin_cannot_deactivate_themselves(admin, expect):
    response = admin.post("/accounts/root/deactivate")
    assert response.status_code == 409
    assert "your own account" in response.json()["detail"]
    assert admin.get("/auth/me").status_code == 200


def test_deactivating_an_unknown_account(admin):
    response = admin.post("/accounts/nobody/deactivate")
    assert response.status_code == 404
    assert response.json()["detail"] == "There is no account named 'nobody'."


def test_resetting_a_password(admin, ana, client, expect, password, new_password):
    secret = new_password()
    assert ana.get("/auth/me").status_code == 200
    assert admin.post("/accounts/ana/password", {"password": secret}).status_code == 204
    # the old login ends with the old password
    assert ana.get("/auth/me").status_code == 401
    old = client.post("/auth/login", json={"username": "ana", "password": password})
    assert old.status_code == 401
    new = client.post("/auth/login", json={"username": "ana", "password": secret})
    assert new.status_code == 200


def test_resetting_the_password_of_an_unknown_account(admin, new_password):
    response = admin.post("/accounts/nobody/password", {"password": new_password()})
    assert response.status_code == 404


def test_a_reset_password_must_be_long_enough(admin, ana):
    response = admin.post("/accounts/ana/password", {"password": "x" * 5})
    assert response.status_code == 422


def stored_hash(settings, username):
    engine = connect(settings.database_uri)
    try:
        with engine.connect() as connection:
            query = sa.text("SELECT password_hash FROM accounts WHERE user_id = :u")
            return connection.execute(query, {"u": username}).scalar_one()
    finally:
        engine.dispose()


def test_passwords_are_stored_as_salted_scrypt_hashes(ana, ben, settings, password):
    first, second = stored_hash(settings, "ana"), stored_hash(settings, "ben")
    assert first.startswith("scrypt$4$8$1$")
    assert password not in first
    assert first != second  # the same password, a different salt
    assert passwords.verify_password(password, first)
    assert passwords.verify_password(password, second)


def test_the_default_cost_is_the_recommended_one(password):
    stored = passwords.hash_password(password)
    assert stored.split("$")[:4] == ["scrypt", "14", "8", "1"]
    assert passwords.verify_password(password, stored)
    assert password.swapcase() != password  # it starts with letters
    assert not passwords.verify_password(password.swapcase(), stored)


def test_a_hash_that_cannot_be_read_never_matches(password):
    for stored in ("", "plain", "scrypt$x$8$1$aa$bb", "md5$14$8$1$aa$bb", "scrypt$14$8$1$!!$??"):
        assert not passwords.verify_password(password, stored)


def test_the_comparison_is_made_in_constant_time(
    client, admin, monkeypatch, password, new_password
):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(passwords.hmac, "compare_digest", spy)
    client.post("/auth/login", json={"username": "root", "password": password})
    client.post("/auth/login", json={"username": "nobody", "password": new_password()})
    assert len(calls) == 2  # an unknown user is checked against a decoy hash
