import json
import pytest
from unittest.mock import patch
from app import app, init_db
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Use dates relative to today (venue time) so tests never fall into the past.
FUTURE_DATE = (datetime.now(ZoneInfo("America/Chicago")).date() + timedelta(days=30)).isoformat()


@pytest.fixture(autouse=True)
def app_context(tmp_path):
    app.config["DATABASE"] = str(tmp_path / "test.db")
    app.config["TESTING"] = True
    app.config["ADMIN_USERNAME"] = "admin"
    app.config["ADMIN_PASSWORD"] = "admin"
    app.config["STAFF_USERNAME"] = "staff"
    app.config["STAFF_PASSWORD"] = "staff"
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


def mock_login_customer(client, email, name):
    with client.session_transaction() as sess:
        from services.auth import create_user, get_user_by_email

        with app.app_context():
            user = get_user_by_email(email)
            if not user:
                user = create_user(email=email, password=None, name=name, google_id=f"google-{email}")

        sess["role"] = "customer"
        sess["user_id"] = user["id"]
        sess["user_name"] = user["name"]
        sess["user_email"] = user["email"]
        return user["id"]


def test_notification_creation_and_retrieval(client):
    # Register/login customer
    mock_login_customer(client, "test_notif@example.com", "Notif User")

    # Create request
    req_resp = client.post(
        "/api/requests",
        data=json.dumps(
            {
                "date": FUTURE_DATE,
                "start_time": "12:00",
                "end_time": "14:00",
                "num_people": 4,
                "contact_name": "Notif User",
                "contact_phone": "12345678",
                "contact_email": "test_notif@example.com",
                "room_id": 1,
            }
        ),
        content_type="application/json",
    )
    assert req_resp.status_code == 201
    res_id = req_resp.get_json()["reservation"]["id"]

    # Sign out customer, sign in staff to approve
    client.post("/logout")
    login_staff(client)

    # Approve request
    app_resp = client.post(f"/api/requests/{res_id}/approve")
    assert app_resp.status_code == 200

    # Sign out staff, sign in customer
    client.post("/logout")
    mock_login_customer(client, "test_notif@example.com", "Notif User")

    # Retrieve notifications
    notif_resp = client.get("/api/notifications")
    assert notif_resp.status_code == 200
    notifs = notif_resp.get_json()["notifications"]
    assert len(notifs) == 1
    assert "APPROVED" in notifs[0]["message"]
    assert notifs[0]["read"] == 0
    notif_id = notifs[0]["id"]

    # Mark read
    read_resp = client.post(f"/api/notifications/{notif_id}/read")
    assert read_resp.status_code == 200

    # Retrieve again (should be deleted/empty now)
    notif_resp2 = client.get("/api/notifications")
    notifs2 = notif_resp2.get_json()["notifications"]
    assert len(notifs2) == 0


def test_preferences_endpoint(client):
    mock_login_customer(client, "pref@example.com", "Pref User")

    # Check default pref is True/1
    me_resp = client.get("/api/me")
    assert me_resp.get_json()["email_notifications"] is True

    # Toggle pref
    pref_resp = client.patch(
        "/api/me/preferences", data=json.dumps({"email_notifications": False}), content_type="application/json"
    )
    assert pref_resp.status_code == 200

    # Check updated pref is False
    me_resp2 = client.get("/api/me")
    assert me_resp2.get_json()["email_notifications"] is False


@patch("services.email.send_confirmation")
def test_email_preferences_check(mock_send_confirmation, client):
    # Create user with email pref turned OFF
    mock_login_customer(client, "no_email@example.com", "No Email User")

    # Toggle preference off
    client.patch(
        "/api/me/preferences", data=json.dumps({"email_notifications": False}), content_type="application/json"
    )

    # Submit booking request
    req_resp = client.post(
        "/api/requests",
        data=json.dumps(
            {
                "date": FUTURE_DATE,
                "start_time": "14:00",
                "end_time": "16:00",
                "num_people": 4,
                "contact_name": "No Email User",
                "contact_phone": "12345678",
                "contact_email": "no_email@example.com",
                "room_id": 1,
            }
        ),
        content_type="application/json",
    )
    res_id = req_resp.get_json()["reservation"]["id"]

    # Log out customer, login staff
    client.post("/logout")
    login_staff(client)

    # Approve booking
    client.post(f"/api/requests/{res_id}/approve")

    # Verify email confirmation service was NOT called
    assert not mock_send_confirmation.called


def test_customer_bookings_and_cancel(client):
    mock_login_customer(client, "bookings@example.com", "Booking User")

    # Create request
    req_resp = client.post(
        "/api/requests",
        data=json.dumps(
            {
                "date": FUTURE_DATE,
                "start_time": "16:00",
                "end_time": "18:00",
                "num_people": 4,
                "contact_name": "Booking User",
                "contact_phone": "12345678",
                "contact_email": "bookings@example.com",
                "room_id": 1,
            }
        ),
        content_type="application/json",
    )
    res_id = req_resp.get_json()["reservation"]["id"]

    # Get bookings
    bookings_resp = client.get("/api/me/bookings")
    assert bookings_resp.status_code == 200
    bookings = bookings_resp.get_json()["bookings"]
    assert len(bookings) == 1
    assert bookings[0]["id"] == res_id
    assert bookings[0]["status"] == "pending"

    # Cancel pending booking
    cancel_resp = client.post(f"/api/me/bookings/{res_id}/cancel")
    assert cancel_resp.status_code == 200

    # Verify booking status in database is cancelled
    bookings_resp2 = client.get("/api/me/bookings")
    bookings2 = bookings_resp2.get_json()["bookings"]
    assert bookings2[0]["status"] == "cancelled"
