import json
import pytest
from app import app, init_db
from services.db import get_db
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Use dates relative to today (venue time) so tests never fall into the past.
FUTURE_DATE = (datetime.now(ZoneInfo("America/Chicago")).date() + timedelta(days=30)).isoformat()
NEXT_DAY = (datetime.now(ZoneInfo("America/Chicago")).date() + timedelta(days=31)).isoformat()


@pytest.fixture(autouse=True)
def app_context(tmp_path):
    app.config["DATABASE"] = str(tmp_path / "test.db")
    app.config["TESTING"] = True
    app.config["ADMIN_USERNAME"] = "admin"
    app.config["ADMIN_PASSWORD"] = "admin"
    with app.app_context():
        init_db()
    yield


@pytest.fixture
def client():
    return app.test_client()


def test_get_messages_generates_session_id(client):
    # Ensure guest_session_id is generated in session
    resp = client.get("/api/messages")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "session_id" in data
    assert "messages" in data
    assert data["messages"] == []

    # Session transaction should contain guest_session_id
    with client.session_transaction() as sess:
        assert "guest_session_id" in sess
        assert sess["guest_session_id"] == data["session_id"]


def test_post_message_guest_and_customer(client):
    # 1. Post as Guest
    resp = client.post(
        "/api/messages",
        data=json.dumps({"message": "Hello, I am a guest", "guest_name": "John", "guest_email": "john@example.com"}),
        content_type="application/json",
    )
    assert resp.status_code == 200

    # Retrieve messages
    resp = client.get("/api/messages")
    data = resp.get_json()
    assert len(data["messages"]) == 1
    msg = data["messages"][0]
    assert msg["message"] == "Hello, I am a guest"
    assert msg["sender_role"] == "guest"
    assert msg["guest_name"] == "John"
    assert msg["guest_email"] == "john@example.com"
    assert msg["user_id"] is None

    # 2. Post as Customer
    # Create customer account
    conn = get_db()
    conn.execute("INSERT INTO users (id, name, email) VALUES (1, 'Jane Customer', 'jane@example.com')")
    conn.commit()
    conn.close()

    with client.session_transaction() as sess:
        sess["role"] = "customer"
        sess["user_id"] = 1
        sess["user_name"] = "Jane Customer"
        sess["user_email"] = "jane@example.com"

    resp = client.post(
        "/api/messages",
        data=json.dumps({"message": "Hello, I am Jane"}),
        content_type="application/json",
    )
    assert resp.status_code == 200

    resp = client.get("/api/messages")
    data = resp.get_json()
    # Should see both messages since they share same guest_session_id (or user_id = 1)
    assert len(data["messages"]) == 2
    msg2 = data["messages"][1]
    assert msg2["message"] == "Hello, I am Jane"
    assert msg2["sender_role"] == "customer"
    assert msg2["user_id"] == 1


def test_staff_messaging_flow(client):
    # 1. Post as guest
    client.post(
        "/api/messages",
        data=json.dumps({"message": "Help please", "guest_name": "Bob"}),
        content_type="application/json",
    )

    with client.session_transaction() as sess:
        session_id = sess["guest_session_id"]

    # 2. Staff views conversation list
    # Unauthenticated staff should be blocked
    resp = client.get("/api/staff/messages")
    assert resp.status_code == 401

    # Authenticate staff
    with client.session_transaction() as sess:
        sess["role"] = "staff"

    resp = client.get("/api/staff/messages")
    assert resp.status_code == 200
    convs = resp.get_json()["conversations"]
    assert len(convs) == 1
    assert convs[0]["session_id"] == session_id
    assert convs[0]["name"] == "Bob"
    assert convs[0]["unread_count"] == 1
    assert convs[0]["last_message"] == "Help please"

    # 3. Staff views specific conversation (marks as read by staff)
    resp = client.get(f"/api/staff/messages/{session_id}")
    assert resp.status_code == 200
    detail = resp.get_json()
    assert detail["name"] == "Bob"
    assert len(detail["messages"]) == 1
    assert detail["messages"][0]["message"] == "Help please"

    # Verify unread count is now 0
    resp = client.get("/api/staff/messages")
    convs = resp.get_json()["conversations"]
    assert convs[0]["unread_count"] == 0

    # 4. Staff replies
    resp = client.post(
        f"/api/staff/messages/{session_id}",
        data=json.dumps({"message": "Hello Bob, how can I help you?"}),
        content_type="application/json",
    )
    assert resp.status_code == 200

    # 5. Customer views messages (marks staff reply as read by user)
    # Switch back to guest
    with client.session_transaction() as sess:
        sess["role"] = "guest"
        sess.pop("user_id", None)
        # Restore session ID
        sess["guest_session_id"] = session_id

    resp = client.get("/api/messages")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["messages"]) == 2
    assert data["messages"][1]["message"] == "Hello Bob, how can I help you?"
    assert data["messages"][1]["sender_role"] == "staff"


def test_login_links_guest_chat(client):
    # Guest posts message
    client.post(
        "/api/messages",
        data=json.dumps({"message": "Guest message"}),
        content_type="application/json",
    )

    with client.session_transaction() as sess:
        guest_sid = sess["guest_session_id"]

    # Verify user_id is None in db
    conn = get_db()
    row = conn.execute("SELECT user_id FROM direct_messages WHERE session_id = ?", (guest_sid,)).fetchone()
    assert row["user_id"] is None

    # Create customer account
    conn.execute(
        "INSERT INTO users (id, name, email, google_id) VALUES (42, 'Google User', 'guser@example.com', 'google123')"
    )
    conn.commit()
    conn.close()

    # Mock Google Login callback using a custom test callback mock or set state
    with client.session_transaction() as sess:
        sess["oauth_state"] = "teststate"

    # Mock exchange_google_code and get_google_user_info
    import unittest.mock as mock

    with mock.patch("services.oauth.exchange_google_code", return_value={"access_token": "token"}), mock.patch(
        "services.oauth.get_google_user_info",
        return_value={"sub": "google123", "email": "guser@example.com", "name": "Google User"},
    ):
        resp = client.get("/login/google/callback?code=code123&state=teststate")
        assert resp.status_code == 302

    # Now check if direct_messages updated user_id to 42
    conn = get_db()
    row = conn.execute("SELECT user_id FROM direct_messages WHERE session_id = ?", (guest_sid,)).fetchone()
    assert row["user_id"] == 42
    conn.close()


def test_customer_edit_restrictions(client):
    # 1. Create a reservation belonging to user 10
    conn = get_db()
    conn.execute("INSERT INTO users (id, name, email) VALUES (10, 'Owner', 'owner@example.com')")
    conn.execute(
        """
        INSERT INTO reservations (id, user_id, date, room_id, start_time, end_time,
                                  contact_name, contact_phone, contact_email, num_people,
                                  language, status, total_cost, deposit_paid)
        VALUES (99, 10, ?, 1, '12:00', '13:00', 'Owner', '555-1234',
                'owner@example.com', 2, 'en', 'pending', 40.0, 0.0)
        """,
        (FUTURE_DATE,),
    )
    conn.commit()
    conn.close()

    # 2. Get detail: should return is_owner
    # As non-owner (guest)
    resp = client.get("/get_reservation/99")
    assert resp.status_code == 200
    assert resp.get_json()["is_owner"] is False
    # As owner (customer 10)
    with client.session_transaction() as sess:
        sess["role"] = "customer"
        sess["user_id"] = 10

    resp = client.get("/get_reservation/99")
    assert resp.status_code == 200
    assert resp.get_json()["is_owner"] is True

    # 3. Try to update restricted fields as customer owner
    resp = client.patch(
        "/api/reservations/99",
        data=json.dumps({"room_id": 2, "contact_name": "New Owner"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "restricted field" in resp.get_json()["error"]["message"]

    # Try to update date/time
    resp = client.patch(
        "/api/reservations/99",
        data=json.dumps({"date": NEXT_DAY, "contact_name": "New Owner"}),
        content_type="application/json",
    )
    assert resp.status_code == 400

    # Try to update total_cost
    resp = client.patch("/api/reservations/99", data=json.dumps({"total_cost": 50.0}), content_type="application/json")
    assert resp.status_code == 400

    # 4. Update allowed fields (contact details, headcount, language, notes)
    resp = client.patch(
        "/api/reservations/99",
        data=json.dumps(
            {
                "contact_name": "Updated Owner",
                "contact_phone": "555-9999",
                "contact_email": "updated@example.com",
                "num_people": 4,
                "language": "zh",
                "notes": "some notes",
                # Include restricted fields but with identical values (allowed)
                "date": FUTURE_DATE,
                "room_id": 1,
                "start_time": "12:00",
                "end_time": "13:00",
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 200

    # Verify in DB
    conn = get_db()
    res = conn.execute("SELECT * FROM reservations WHERE id = 99").fetchone()
    assert res["contact_name"] == "Updated Owner"
    assert res["contact_phone"] == "555-9999"
    assert res["num_people"] == 4
    assert res["language"] == "zh"
    assert res["notes"] == "some notes"
    # Ensure restricted fields didn't change
    assert res["date"] == FUTURE_DATE
    assert res["room_id"] == 1
    assert res["start_time"] == "12:00"
    conn.close()


def test_staff_delete_conversation(client):
    # 1. Post message as guest to create a conversation
    client.post(
        "/api/messages",
        data=json.dumps({"message": "Message to delete", "guest_name": "DeleteMe"}),
        content_type="application/json",
    )

    with client.session_transaction() as sess:
        session_id = sess["guest_session_id"]

    # 2. Try deleting unauthenticated (should fail)
    resp = client.delete(f"/api/staff/messages/{session_id}")
    assert resp.status_code == 401

    # 3. Login as staff and delete conversation
    with client.session_transaction() as sess:
        sess["role"] = "staff"

    resp = client.delete(f"/api/staff/messages/{session_id}")
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "Conversation deleted successfully"

    # 4. Verify conversation is gone from staff conversations list
    resp = client.get("/api/staff/messages")
    assert resp.status_code == 200
    convs = resp.get_json()["conversations"]
    assert len(convs) == 0

    # 5. Verify direct messages table is empty for this session
    conn = get_db()
    rows = conn.execute("SELECT * FROM direct_messages WHERE session_id = ?", (session_id,)).fetchall()
    assert len(rows) == 0
    conn.close()
