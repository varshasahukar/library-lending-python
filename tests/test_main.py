from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class TestHealth:
    @patch("app.main.cache.client")
    @patch("app.main.db.query")
    def test_health_success(self, mock_query, mock_cache_client):
        mock_query.return_value = [{"?column?": 1}]
        mock_cache_client.return_value.ping.return_value = True

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["postgres"] is True
        assert response.json()["redis"] is True

    @patch("app.main.cache.client")
    @patch("app.main.db.query")
    def test_health_postgres_failure(self, mock_query, mock_cache_client):
        mock_query.side_effect = Exception("Postgres unavailable")
        mock_cache_client.return_value.ping.return_value = True

        response = client.get("/health")

        assert response.status_code == 503
        assert response.json()["postgres"] is False
        assert response.json()["redis"] is True
        assert "pg_error" in response.json()

    @patch("app.main.cache.client")
    @patch("app.main.db.query")
    def test_health_redis_failure(self, mock_query, mock_cache_client):
        mock_query.return_value = [{"?column?": 1}]
        mock_cache_client.return_value.ping.side_effect = Exception(
            "Redis unavailable"
        )

        response = client.get("/health")

        assert response.status_code == 503
        assert response.json()["postgres"] is True
        assert response.json()["redis"] is False
        assert "redis_error" in response.json()


class TestBooks:
    @patch("app.main.db.query")
    def test_list_books(self, mock_query):
        mock_query.return_value = [
            {
                "id": 1,
                "title": "Test Book",
                "author": "Test Author",
                "reference_only": False,
                "copies": 3,
                "available": 2,
            }
        ]

        response = client.get("/books")

        assert response.status_code == 200
        assert response.json()["books"][0]["title"] == "Test Book"

    @patch("app.main._available")
    @patch("app.main.db.one")
    def test_book_availability_not_found(self, mock_one, mock_available):
        mock_one.return_value = None

        response = client.get("/books/999/availability")

        assert response.status_code == 404
        assert response.json()["detail"] == "no such book"
        mock_available.assert_not_called()

    @patch("app.main._available")
    @patch("app.main.db.one")
    def test_book_availability_response(self, mock_one, mock_available):
        mock_one.return_value = {
            "id": 1,
            "title": "Test Book",
            "reference_only": False,
        }
        mock_available.return_value = (2, True)

        response = client.get("/books/1/availability")

        assert response.status_code == 200
        assert response.json() == {
            "book_id": 1,
            "title": "Test Book",
            "available": 2,
            "reference_only": False,
            "cached": True,
        }


class TestMembers:
    @patch("app.main._member_state")
    @patch("app.main.db.one")
    def test_member_not_found(self, mock_one, mock_member_state):
        mock_one.return_value = None

        response = client.get("/members/999")

        assert response.status_code == 404
        assert response.json()["detail"] == "no such member"
        mock_member_state.assert_not_called()

    @patch("app.main.projected_fine")
    @patch("app.main._member_state")
    @patch("app.main.db.one")
    def test_member_details_with_open_loan(
        self, mock_one, mock_member_state, mock_projected_fine
    ):
        mock_one.return_value = {
            "id": 1,
            "name": "Test Member",
            "email": "test@example.com",
        }
        mock_member_state.return_value = (
            [
                {
                    "id": 10,
                    "copy_id": 2,
                    "issued_on": "2026-10-01",
                    "due_on": "2026-10-05",
                    "returned_on": None,
                    "renewals": 0,
                }
            ],
            [],
            {"active_loans": 1, "unpaid_fines": 0, "overdue_loans": 0},
        )
        mock_projected_fine.return_value = {"amount": 2.0, "days": 2}

        response = client.get("/members/1")

        assert response.status_code == 200
        data = response.json()
        assert data["member"]["name"] == "Test Member"
        assert data["loans"][0]["fine_if_returned_today"]["amount"] == 2.0
        mock_projected_fine.assert_called_once()

    @patch("app.main._member_state")
    @patch("app.main.db.one")
    def test_member_with_returned_loan(
        self, mock_one, mock_member_state
    ):
        mock_one.return_value = {
            "id": 2,
            "name": "Returned Loan Member",
            "email": "returned@example.com",
        }
        mock_member_state.return_value = (
            [
                {
                    "id": 11,
                    "copy_id": 3,
                    "issued_on": "2026-09-01",
                    "due_on": "2026-09-15",
                    "returned_on": "2026-09-10",
                    "renewals": 0,
                }
            ],
            [],
            {"active_loans": 0, "unpaid_fines": 0, "overdue_loans": 0},
        )

        response = client.get("/members/2")

        assert response.status_code == 200
        assert "fine_if_returned_today" not in response.json()["loans"][0]


class TestBorrow:
    def test_borrow_invalid_payload(self):
        response = client.post("/borrow", json={"member_id": "invalid", "book_id": 1})

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "member_id and book_id are required integers"
        )

    @patch("app.main.db.one")
    def test_borrow_member_not_found(self, mock_one):
        mock_one.return_value = None

        response = client.post(
            "/borrow", json={"member_id": 999, "book_id": 1}
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "no such member"

    @patch("app.main.db.one")
    def test_borrow_book_not_found(self, mock_one):
        mock_one.side_effect = [{"id": 1}, None]

        response = client.post(
            "/borrow", json={"member_id": 1, "book_id": 999}
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "no such book"

    @patch("app.main._available")
    @patch("app.main._member_state")
    @patch("app.main.db.one")
    def test_borrow_reference_only_book(
        self, mock_one, mock_member_state, mock_available
    ):
        mock_one.side_effect = [
            {"id": 1},
            {"id": 6, "title": "Reference Book", "reference_only": True},
        ]
        mock_member_state.return_value = (
            [],
            [],
            {"active_loans": 0, "unpaid_fines": 0, "overdue_loans": 0},
        )
        mock_available.return_value = (1, False)

        response = client.post("/borrow", json={"member_id": 1, "book_id": 6})

        assert response.status_code == 409
        assert response.json()["detail"] == "reference_only"

    @patch("app.main._lock")
    @patch("app.main._available")
    @patch("app.main._member_state")
    @patch("app.main.db.one")
    def test_borrow_lock_conflict(
        self, mock_one, mock_member_state, mock_available, mock_lock
    ):
        mock_one.side_effect = [
            {"id": 1},
            {"id": 2, "title": "Regular Book", "reference_only": False},
        ]
        mock_member_state.return_value = (
            [],
            [],
            {"active_loans": 0, "unpaid_fines": 0, "overdue_loans": 0},
        )
        mock_available.return_value = (1, False)
        mock_lock.return_value = False

        response = client.post("/borrow", json={"member_id": 1, "book_id": 2})

        assert response.status_code == 409
        assert response.json()["detail"] == "another borrow for this book is in flight"


class TestReturn:
    def test_return_invalid_loan_id(self):
        response = client.post("/return", json={"loan_id": "invalid"})

        assert response.status_code == 400
        assert response.json()["detail"] == "loan_id is required"

    @patch("app.main.db.one")
    def test_return_loan_not_found(self, mock_one):
        mock_one.return_value = None

        response = client.post("/return", json={"loan_id": 999})

        assert response.status_code == 404
        assert response.json()["detail"] == "no such loan"

    @patch("app.main.db.one")
    def test_return_already_returned(self, mock_one):
        mock_one.return_value = {
            "id": 1,
            "copy_id": 2,
            "member_id": 3,
            "due_on": "2026-10-01",
            "returned_on": "2026-10-02",
            "book_id": 4,
        }

        response = client.post("/return", json={"loan_id": 1})

        assert response.status_code == 409
        assert response.json()["detail"] == "that loan was already returned"


class TestRenew:
    @patch("app.main.db.one")
    def test_renew_loan_not_found(self, mock_one):
        mock_one.return_value = None

        response = client.post("/loans/999/renew")

        assert response.status_code == 404
        assert response.json()["detail"] == "no such loan"

    @patch("app.main.db.one")
    def test_renew_closed_loan(self, mock_one):
        mock_one.return_value = {
            "id": 1,
            "member_id": 2,
            "due_on": "2026-10-01",
            "returned_on": "2026-10-02",
            "renewals": 0,
        }

        response = client.post("/loans/1/renew")

        assert response.status_code == 409
        assert response.json()["detail"] == "that loan is closed"


class TestPayFines:
    @patch("app.main.db.one")
    def test_pay_member_not_found(self, mock_one):
        mock_one.return_value = None

        response = client.post("/members/999/fines/pay")

        assert response.status_code == 404
        assert response.json()["detail"] == "no such member"

    @patch("app.main.db.query")
    @patch("app.main.db.one")
    def test_pay_no_unpaid_fines(self, mock_one, mock_query):
        mock_one.return_value = {"id": 1}
        mock_query.return_value = []

        response = client.post("/members/1/fines/pay")

        assert response.status_code == 409
        assert response.json()["detail"] == "nothing to pay"

    @patch("app.main.db.query")
    @patch("app.main.db.one")
    def test_pay_fines_success(self, mock_one, mock_query):
        mock_one.return_value = {"id": 1}
        mock_query.return_value = [
            {"id": 1, "amount": 2.25},
            {"id": 2, "amount": 1.75},
        ]

        response = client.post("/members/1/fines/pay")

        assert response.status_code == 200
        assert response.json()["member_id"] == 1
        assert response.json()["cleared"] == 2
        assert response.json()["paid"] == 4.0
