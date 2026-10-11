"""Logging in and out, and what happens without a valid login."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import replace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from labelflow.api import create_app, identity
from labelflow.store import connect

NOT_LOGGED_IN = "You are not logged in. Log in with POST /auth/login."


def login(client, username, password, status=200):
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == status, response.text
    return response


def test_login_gives_a_token_and_a_cookie(client, admin, expect, password):
    response = login(client, "root", password)
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"] == {"user_id": "root", "display_name": "Root", "is_admin": True}
    assert body["expires_at"].startswith("2026-10-10T10:00:00")  # one hour after the test clock
    cookie = response.headers["set-cookie"]
    assert f"labelflow_session={body['token']}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=3600" in cookie
    assert "Secure" not in cookie


def test_the_cookie_alone_logs_you_in(app, admin, password):
    with TestClient(app) as browser:  # a client with its own cookie jar
        login(browser, "root", password)
        assert browser.get("/auth/me").json()["user_id"] == "root"


def test_the_bearer_token_logs_you_in(admin, expect):
    assert expect(admin.get("/auth/me")) == {
        "user_id": "root",
        "display_name": "Root",
        "is_admin": True,
    }


def test_the_cookie_is_marked_secure_when_asked(settings, label_tasks, admin, clock, password):
    with TestClient(
        create_app(replace(settings, cookie_secure=True), label_tasks=label_tasks)
    ) as c:
        response = login(c, "root", password)
        assert "Secure" in response.headers["set-cookie"]


def test_a_wrong_password_and_an_unknown_user_get_the_same_answer(
    client, admin, password, new_password
):
    wrong = login(client, "root", new_password(), 401)
    unknown = login(client, "nobody", password, 401)
    assert wrong.json() == unknown.json()
    assert wrong.json()["detail"] == "Wrong username or password."
    assert wrong.headers["www-authenticate"] == "Bearer"
    assert "set-cookie" not in wrong.headers


def test_login_needs_both_fields(client):
    response = client.post("/auth/login", json={"username": "root"})
    assert response.status_code == 422
    assert "password: Field required" in response.json()["problems"]


def test_a_deactivated_account_cannot_log_in(client, admin, ana, expect, password, new_password):
    expect(admin.post("/accounts/ana/deactivate"))
    response = login(client, "ana", password, 401)
    assert response.json()["detail"] == "This account has been deactivated. Ask an administrator."
    # the message is only given to someone who knows the password
    assert (
        login(client, "ana", new_password(), 401).json()["detail"] == "Wrong username or password."
    )


def test_no_token_is_refused(anon, expect):
    response = anon.get("/auth/me")
    assert response.status_code == 401
    assert response.json() == {"detail": NOT_LOGGED_IN, "problems": []}
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_made_up_token_is_refused(client):
    response = client.get("/auth/me", headers={"Authorization": "Bearer made-up"})
    assert response.status_code == 401
    assert "not valid any more" in response.json()["detail"]


def test_a_bad_bearer_token_is_not_rescued_by_a_good_cookie(app, admin, password):
    with TestClient(app) as browser:
        login(browser, "root", password)
        assert browser.get("/auth/me").status_code == 200
        bad = browser.get("/auth/me", headers={"Authorization": "Bearer made-up"})
        assert bad.status_code == 401


def test_a_login_expires(admin, clock, expect):
    assert admin.get("/auth/me").status_code == 200
    clock.advance(minutes=59)
    assert admin.get("/auth/me").status_code == 200
    clock.advance(minutes=2)
    response = admin.get("/auth/me")
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_an_expired_login_is_removed_from_the_database(admin, settings, clock):
    clock.advance(hours=2)
    assert admin.get("/auth/me").status_code == 401
    assert sessions(settings) == []


def test_a_deactivated_account_loses_its_login_at_once(admin, ana, expect):
    assert ana.get("/auth/me").status_code == 200
    expect(admin.post("/accounts/ana/deactivate"))
    response = ana.get("/auth/me")
    assert response.status_code == 401
    assert "deactivated" in response.json()["detail"]


def test_logging_out_ends_the_login(app, admin, settings, password):
    with TestClient(app) as browser:
        token = login(browser, "root", password).json()["token"]
        response = browser.post("/auth/logout")
        assert response.status_code == 204
        assert "labelflow_session=" in response.headers["set-cookie"]
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert browser.get("/auth/me").status_code == 401
        assert (
            browser.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        )


def test_logging_out_with_a_bearer_token(admin, client):
    assert admin.post("/auth/logout").status_code == 204
    assert admin.get("/auth/me").status_code == 401


def test_logging_out_when_not_logged_in_is_harmless(anon):
    assert anon.post("/auth/logout").status_code == 204


def test_a_login_does_not_end_the_others(client, admin, password):
    first = login(client, "root", password).json()["token"]
    second = login(client, "root", password).json()["token"]
    assert first != second
    client.cookies.clear()
    for token in (first, second, admin.token):
        assert (
            client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        )


def sessions(settings):
    engine = connect(settings.database_uri)
    try:
        with engine.connect() as connection:
            return list(connection.execute(sa.text("SELECT * FROM sessions")).mappings())
    finally:
        engine.dispose()


def test_only_a_fingerprint_of_the_token_is_stored(admin, settings):
    rows = sessions(settings)
    assert len(rows) == 1
    assert rows[0]["token_hash"] == hashlib.sha256(admin.token.encode()).hexdigest()
    assert admin.token not in "".join(str(value) for value in rows[0].values())
    assert identity.token_hash(admin.token) == rows[0]["token_hash"]


ADMIN_ENDPOINTS = [
    ("GET", "/accounts", None),
    ("POST", "/accounts", {"username": "cy", "password": secrets.token_urlsafe(16)}),
    ("POST", "/accounts/ana/deactivate", None),
    ("POST", "/accounts/ana/password", {"password": secrets.token_urlsafe(16)}),
    ("POST", "/projects", {"name": "other"}),
    ("GET", "/projects/xray/roles", None),
    ("POST", "/projects/xray/roles", {"role": "trainee"}),
    ("POST", "/projects/xray/roles/annotator/grant", {"user": "ben", "reason": "x"}),
    ("POST", "/projects/xray/roles/annotator/revoke", {"user": "ben", "reason": "x"}),
    ("GET", "/projects/xray/datasets", None),
    ("POST", "/projects/xray/datasets", {"name": "more"}),
    ("POST", "/projects/xray/datasets/batch1/items", {"items": []}),
    ("GET", "/projects/xray/datasets/batch1/items", None),
    ("POST", "/projects/xray/workflows/validate", {"config": "x: 1"}),
    ("POST", "/projects/xray/workflows", {"config": "x: 1"}),
    ("GET", "/projects/xray/workflows/xray_main/versions", None),
    ("POST", "/projects/xray/workflows/xray_main/preview-edit", {"config": "x: 1"}),
    ("POST", "/projects/xray/workflows/xray_main/edits", {"config": "x: 1", "base_version": 1}),
    ("POST", "/projects/xray/workflows/xray_main/items/img_001/exclude", {"reason": "x"}),
    (
        "POST",
        "/projects/xray/workflows/xray_main/stages/annotate/cancel-open-slots",
        {"reason": "x"},
    ),
    ("GET", "/projects/xray/workflows/xray_main/items/img_001/explain", None),
    ("GET", "/projects/xray/workflows/xray_main/reports/scores", None),
    ("GET", "/projects/xray/workflows/xray_main/reports/sampling", None),
    ("GET", "/projects/xray/workflows/xray_main/exports/items", None),
    ("GET", "/projects/xray/workflows/xray_main/exports/labels", None),
    ("GET", "/projects/xray/events", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
def test_a_labeler_cannot_use_an_admin_endpoint(ana, method, path, body):
    response = ana.request(method, path, json=body)
    assert response.status_code == 403, (method, path, response.text)
    assert response.json() == {"detail": "This needs an administrator account.", "problems": []}


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
def test_every_endpoint_needs_a_login(anon, method, path, body):
    response = anon.request(method, path, json=body)
    assert response.status_code == 401, (method, path, response.text)


LABELER_ENDPOINTS = [
    ("GET", "/projects"),
    ("GET", "/projects/xray/workflows"),
    ("GET", "/projects/xray/workflows/xray_main"),
    ("POST", "/projects/xray/workflows/xray_main/next"),
    ("POST", "/projects/xray/workflows/xray_main/slots/1/claim"),
    ("GET", "/projects/xray/workflows/xray_main/slots/1"),
    ("POST", "/projects/xray/workflows/xray_main/slots/1/submit"),
    ("POST", "/projects/xray/workflows/xray_main/slots/1/release"),
]


@pytest.mark.parametrize(("method", "path"), LABELER_ENDPOINTS)
def test_the_labeler_endpoints_need_a_login(anon, method, path):
    assert anon.request(method, path).status_code == 401


def test_an_unknown_project_is_not_found_for_an_admin_and_forbidden_for_a_labeler(admin, ana):
    assert admin.get("/projects/nope/roles").status_code == 404
    assert ana.get("/projects/nope/roles").status_code == 403
