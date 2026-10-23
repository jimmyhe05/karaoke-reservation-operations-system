import json
import pytest
from datetime import datetime as real_datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from app import app, init_db
from services.db import get_db

# Use dates relative to today (venue time) so tests never fall into the past.
FUTURE_DATE = (real_datetime.now(ZoneInfo("America/Chicago")).date() + timedelta(days=30)).isoformat()


@pytest.fixture(autouse=True)
def app_context(tmp_path):
    app.config["DATABASE"] = str(tmp_path / "test.db")
    app.config["TESTING"] = True
    app.config["ADMIN_USERNAME"] = "admin"
    app.config["ADMIN_PASSWORD"] = "admin"
    app.config["STAFF_USERNAME"] = "staff"
    app.config["STAFF_PASSWORD"] = "staff"
    app.config["GOOGLE_CLIENT_ID"] = "mock-client-id"
    app.config["GOOGLE_CLIENT_SECRET"] = "mock-client-secret"
    app.config["BASE_URL"] = "http://localhost:5000"
    app.config["CANCEL_CUTOFF_HOURS"] = 2
    with app.app_context():
        init_db()
    yield


@pytest.fixture
def client():
    return app.test_client()


def login_staff(client):
    return client.post(
        "/login",
        data=json.dumps({"username": "staff", "password": "staff"}),
        content_type="application/json",
    )


def mock_login_customer(client, email, name, google_id=None):
    """Helper to mock a Google logged-in customer session."""
    if google_id is None:
        google_id = f"google-{email}"
    with client.session_transaction() as sess:
        from services.auth import create_user, get_user_by_email

        with app.app_context():
            user = get_user_by_email(email)
            if not user:
                user = create_user(email=email, password=None, name=name, google_id=google_id)

        sess["role"] = "customer"
        sess["user_id"] = user["id"]
        sess["user_name"] = user["name"]
        sess["user_email"] = user["email"]


# --- Test Google OAuth Callback Flow ---


@patch("services.oauth.exchange_google_code")
@patch("services.oauth.get_google_user_info")
def test_google_oauth_callback_flow(mock_user_info, mock_exchange, client):
    # Setup mock returns
    mock_exchange.return_value = {"access_token": "mock-access-token"}
    mock_user_info.return_value = {"sub": "google-12345", "email": "oauth@example.com", "name": "Oauth User"}

    # First get the /login/google to set up state
    resp = client.get("/login/google")
    assert resp.status_code == 302
    assert "accounts.google.com" in resp.headers["Location"]

    # Retrieve oauth_state from the session cookie
    with client.session_transaction() as sess:
        state = sess.get("oauth_state")

    # Access callback route
    callback_resp = client.get(f"/login/google/callback?code=mock-code&state={state}")
    assert callback_resp.status_code == 302
    assert callback_resp.headers["Location"] == "/"

    # Verify user was created and session is populated
    me_resp = client.get("/api/me")
    me_data = me_resp.get_json()
    assert me_data["role"] == "customer"
    assert me_data["email"] == "oauth@example.com"
    assert me_data["name"] == "Oauth User"


# --- Test Request Creation and Overlaps ---


def test_pending_request_creation_and_overlapping(client):
    # Guest cannot create request
    request_payload = {
        "date": FUTURE_DATE,
        "start_time": "12:00",
        "end_time": "13:00",
        "num_people": 4,
        "contact_name": "Bob",
        "contact_phone": "555-1234",
        "contact_email": "bob@example.com",
        "room_id": 1,
        "notes": "Testing overlap",
    }
    guest_resp = client.post("/api/requests", data=json.dumps(request_payload), content_type="application/json")
    assert guest_resp.status_code == 401

    # Login customer 1 via mock
    mock_login_customer(client, "bob@example.com", "Bob")

    # Make request
    resp1 = client.post("/api/requests", data=json.dumps(request_payload), content_type="application/json")
    assert resp1.status_code == 201
    req1 = resp1.get_json()["reservation"]
    assert req1["status"] == "pending"
    assert req1["id"] is not None

    # Login customer 2 via mock
    mock_login_customer(client, "charlie@example.com", "Charlie")

    # Make overlapping request (exact same slot)
    request_payload_2 = request_payload.copy()
    request_payload_2["contact_name"] = "Charlie"
    request_payload_2["contact_email"] = "charlie@example.com"

    resp2 = client.post("/api/requests", data=json.dumps(request_payload_2), content_type="application/json")
    # Overlapping pending requests should NOT conflict!
    assert resp2.status_code == 201
    req2 = resp2.get_json()["reservation"]
    assert req2["status"] == "pending"


# --- Test Staff Pending Inbox, Approving, and Declining ---


def test_staff_approval_and_rejection_conflict_handling(client):
    # Setup two overlapping requests
    mock_login_customer(client, "bob@example.com", "Bob")
    req_payload = {
        "date": FUTURE_DATE,
        "start_time": "12:00",
        "end_time": "13:00",
        "num_people": 4,
        "contact_name": "Bob",
        "contact_phone": "555-1234",
        "contact_email": "bob@example.com",
        "room_id": 1,
    }
    resp1 = client.post("/api/requests", data=json.dumps(req_payload), content_type="application/json")
    id1 = resp1.get_json()["reservation"]["id"]

    mock_login_customer(client, "charlie@example.com", "Charlie")
    resp2 = client.post("/api/requests", data=json.dumps(req_payload), content_type="application/json")
    id2 = resp2.get_json()["reservation"]["id"]

    # Customer tries to access staff pending list -> returns 403
    pending_resp = client.get("/api/requests/pending")
    assert pending_resp.status_code == 403

    # Logout customer to make request as guest -> returns 401
    client.post("/api/auth/logout")
    pending_resp_guest = client.get("/api/requests/pending")
    assert pending_resp_guest.status_code == 401

    # Staff logins
    login_staff(client)

    # Check pending list
    pending_resp = client.get("/api/requests/pending")
    assert pending_resp.status_code == 200
    pending_data = pending_resp.get_json()["requests"]
    assert len(pending_data) == 2

    # Staff approves Bob's request
    approve_resp = client.post(f"/api/requests/{id1}/approve")
    assert approve_resp.status_code == 200

    # Verify Bob's reservation is confirmed and has a cancellation token
    with app.app_context():
        conn = get_db()
        bob_res = conn.execute("SELECT * FROM reservations WHERE id = ?", (id1,)).fetchone()
        assert bob_res["status"] == "confirmed"
        assert bob_res["cancellation_token"] is not None

    # Staff tries to approve Charlie's request, but it conflicts now!
    approve2_resp = client.post(f"/api/requests/{id2}/approve")
    assert approve2_resp.status_code == 409
    assert approve2_resp.get_json()["error"]["code"] == "conflict"

    # Staff declines Charlie's request instead
    decline_resp = client.post(
        f"/api/requests/{id2}/decline",
        data=json.dumps({"reason": "Room already booked"}),
        content_type="application/json",
    )
    assert decline_resp.status_code == 200

    # Verify status is rejected
    with app.app_context():
        conn = get_db()
        charlie_res = conn.execute("SELECT * FROM reservations WHERE id = ?", (id2,)).fetchone()
        assert charlie_res["status"] == "rejected"


# --- Test Cancellation cutoff and flow ---


class MockDatetime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        dt = real_datetime(2026, 6, 22, 12, 0, 0)
        if tz is not None:
            return dt.replace(tzinfo=tz)
        return dt

    @classmethod
    def utcnow(cls):
        return real_datetime(2026, 6, 22, 12, 0, 0)


@patch("routes.api.datetime", MockDatetime)
@patch("services.validation.datetime", MockDatetime)
@patch("services.reservations.datetime", MockDatetime)
def test_cancellation_flow_and_cutoff(client):
    mock_login_customer(client, "bob@example.com", "Bob")

    # 1. Create a reservation that starts in 4 hours relative to our mock now (12:00) -> 16:00
    chicago_tz = ZoneInfo("America/Chicago")
    from datetime import datetime as real_datetime

    dt_outside = real_datetime(2026, 6, 22, 16, 0, 0, tzinfo=chicago_tz)
    date_str_outside = dt_outside.strftime("%Y-%m-%d")
    start_str_outside = dt_outside.strftime("%H:%M")
    end_str_outside = (dt_outside + timedelta(hours=1)).strftime("%H:%M")

    req_outside = {
        "date": date_str_outside,
        "start_time": start_str_outside,
        "end_time": end_str_outside,
        "num_people": 4,
        "contact_name": "Bob",
        "contact_phone": "555-1234",
        "contact_email": "bob@example.com",
        "room_id": 1,
    }
    resp_outside = client.post("/api/requests", data=json.dumps(req_outside), content_type="application/json")
    id_outside = resp_outside.get_json()["reservation"]["id"]

    # 2. Create a reservation that starts in 1 hour relative to our mock now (12:00) -> 13:00
    dt_inside = real_datetime(2026, 6, 22, 13, 0, 0, tzinfo=chicago_tz)
    date_str_inside = dt_inside.strftime("%Y-%m-%d")
    start_str_inside = dt_inside.strftime("%H:%M")
    end_str_inside = (dt_inside + timedelta(hours=1)).strftime("%H:%M")

    req_inside = {
        "date": date_str_inside,
        "start_time": start_str_inside,
        "end_time": end_str_inside,
        "num_people": 4,
        "contact_name": "Bob",
        "contact_phone": "555-1234",
        "contact_email": "bob@example.com",
        "room_id": 1,
    }
    resp_inside = client.post("/api/requests", data=json.dumps(req_inside), content_type="application/json")
    id_inside = resp_inside.get_json()["reservation"]["id"]

    # Approve both
    login_staff(client)
    client.post(f"/api/requests/{id_outside}/approve")
    client.post(f"/api/requests/{id_inside}/approve")

    with app.app_context():
        conn = get_db()
        token_outside = conn.execute(
            "SELECT cancellation_token FROM reservations WHERE id = ?", (id_outside,)
        ).fetchone()["cancellation_token"]
        token_inside = conn.execute(
            "SELECT cancellation_token FROM reservations WHERE id = ?", (id_inside,)
        ).fetchone()["cancellation_token"]

    # Test GET cancel confirm endpoints
    get_cancel_outside = client.get(f"/cancel/{token_outside}")
    assert get_cancel_outside.status_code == 200
    assert b"confirm cancellation" in get_cancel_outside.data.lower()

    get_cancel_inside = client.get(f"/cancel/{token_inside}")
    assert get_cancel_inside.status_code == 200
    assert b"cannot cancel" in get_cancel_inside.data.lower()

    # Test POST cancellation (Execution)
    # Outside cutoff (success)
    post_cancel_outside = client.post(f"/cancel/{token_outside}")
    assert post_cancel_outside.status_code == 200

    with app.app_context():
        conn = get_db()
        res_outside = conn.execute("SELECT * FROM reservations WHERE id = ?", (id_outside,)).fetchone()
        assert res_outside["status"] == "cancelled"

    # Inside cutoff (fails)
    post_cancel_inside = client.post(f"/cancel/{token_inside}")
    assert post_cancel_inside.status_code == 409
    assert post_cancel_inside.get_json()["error"]["code"] == "cutoff_exceeded"

    with app.app_context():
        conn = get_db()
        res_inside = conn.execute("SELECT * FROM reservations WHERE id = ?", (id_inside,)).fetchone()
        assert res_inside["status"] == "confirmed"  # Remains confirmed
