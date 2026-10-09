import concurrent.futures
import os
import pytest
from fastapi.testclient import TestClient

from app import cache, db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def ensure_db_and_cache():
    """Ensure database and redis are accessible before running integration tests."""
    if not os.environ.get("DATABASE_URL") or not os.environ.get("REDIS_URL"):
        pytest.skip("DATABASE_URL or REDIS_URL not set; skipping integration tests.")
    try:
        db.query("SELECT 1")
        cache.client().ping()
    except Exception as e:
        pytest.skip(f"Integration dependencies unreachable: {e}")


class TestHealth:
    def test_health_endpoint(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["postgres"] is True
        assert data["redis"] is True


class TestBooks:
    def test_get_books(self):
        response = client.get("/books")
        assert response.status_code == 200
        data = response.json()
        assert "books" in data
        assert len(data["books"]) >= 6
        book_map = {b["id"]: b for b in data["books"]}
        assert 1 in book_map
        assert book_map[1]["title"] == "Designing Data-Intensive Applications"
        assert book_map[3]["title"] == "Operating Systems: Three Easy Pieces"
        assert book_map[6]["reference_only"] is True

    def test_book_availability_caching(self):
        cache.drop("avail:1")
        res1 = client.get("/books/1/availability")
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["book_id"] == 1
        assert data1["cached"] is False

        # Second call should be served from Redis cache
        res2 = client.get("/books/1/availability")
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["book_id"] == 1
        assert data2["cached"] is True
        assert data2["available"] == data1["available"]

    def test_availability_non_existent_book(self):
        response = client.get("/books/99999/availability")
        assert response.status_code == 404
        assert response.json()["detail"] == "no_such_book" or "no such book" in response.json()["detail"]


class TestMembers:
    def test_get_member_details(self):
        # Member 1 is Asha Menon
        response = client.get("/members/1")
        assert response.status_code == 200
        data = response.json()
        assert data["member"]["name"] == "Asha Menon"
        assert "loans" in data
        assert "fines" in data
        assert "summary" in data

    def test_member_with_overdue_and_projected_fine(self):
        # Member 3 is Priya Nair who holds an overdue loan
        response = client.get("/members/3")
        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["overdue_loans"] > 0
        assert data["summary"]["blocked"] is True
        # Check projected fine on open loan
        open_loans = [ln for ln in data["loans"] if not ln["returned_on"]]
        assert len(open_loans) > 0
        assert "fine_if_returned_today" in open_loans[0]
        assert open_loans[0]["fine_if_returned_today"]["amount"] > 0

    def test_get_non_existent_member(self):
        response = client.get("/members/99999")
        assert response.status_code == 404


class TestBorrow:
    def test_borrow_validation_errors(self):
        # Bad payload
        res = client.post("/borrow", json={"invalid": "payload"})
        assert res.status_code == 400

        # Non-existent member
        res = client.post("/borrow", json={"member_id": 9999, "book_id": 1})
        assert res.status_code == 404

        # Non-existent book
        res = client.post("/borrow", json={"member_id": 1, "book_id": 9999})
        assert res.status_code == 404

    def test_borrow_policy_blocks(self):
        # Reference-only book (book 6)
        res = client.post("/borrow", json={"member_id": 1, "book_id": 6})
        assert res.status_code == 409
        assert res.json()["detail"] == "reference_only"

        # Loan limit reached (Vikram, member 2 holds 4 active loans)
        res = client.post("/borrow", json={"member_id": 2, "book_id": 4})
        assert res.status_code == 409
        assert res.json()["detail"] == "loan_limit_reached"

        # Overdue book held (Priya, member 3 has overdue loan)
        res = client.post("/borrow", json={"member_id": 3, "book_id": 4})
        assert res.status_code == 409
        assert res.json()["detail"] == "overdue_book_held"

    def test_successful_borrow(self):
        # Member 1 (Asha) borrows Book 4 (Algorithms has 4 copies)
        res = client.post("/borrow", json={"member_id": 1, "book_id": 4})
        assert res.status_code == 201
        data = res.json()
        assert "loan_id" in data
        assert data["member_id"] == 1
        assert "due_on" in data
        assert "barcode" in data


class TestConcurrencyRaceCondition:
    def test_last_copy_race(self):
        """Two concurrent borrow requests for the single copy of Book 3.

        Book 3 has exactly 1 copy (barcode LIB-0006).
        Exactly ONE request must succeed (201 Created).
        The losing request must receive 409 Conflict.
        """
        # Ensure book 3 copy is available before test
        db.query("UPDATE copies SET status='available' WHERE book_id=3")
        db.query("UPDATE loans SET returned_on=CURRENT_DATE WHERE copy_id=6 AND returned_on IS NULL")
        cache.drop("avail:3")

        results = []

        def attempt_borrow(member_id):
            c = TestClient(app)
            return c.post("/borrow", json={"member_id": member_id, "book_id": 3})

        # Member 1 (Asha) and Member 5 (Fatima) race for the last copy
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(attempt_borrow, 1)
            f2 = executor.submit(attempt_borrow, 5)
            r1 = f1.result()
            r2 = f2.result()
            results = [r1, r2]

        status_codes = [r.status_code for r in results]
        assert 201 in status_codes, f"Expected one 201 success, got {status_codes}"
        assert 409 in status_codes, f"Expected one 409 conflict, got {status_codes}"

        # Verify only 1 active loan exists in DB
        active_loans = db.query(
            "SELECT count(*) AS n FROM loans WHERE copy_id=6 AND returned_on IS NULL"
        )
        assert int(active_loans[0]["n"]) == 1

        # Verify copy status is on_loan
        copy_row = db.one("SELECT status FROM copies WHERE id=6")
        assert copy_row["status"] == "on_loan"


class TestReturnAndRenew:
    def test_return_loan(self):
        # Borrow book 2 (which has an available copy) for Asha (member 1) then return it
        borrow_res = client.post("/borrow", json={"member_id": 1, "book_id": 2})
        assert borrow_res.status_code == 201
        loan_id = borrow_res.json()["loan_id"]

        # Return loan
        ret_res = client.post("/return", json={"loan_id": loan_id})
        assert ret_res.status_code == 200
        ret_data = ret_res.json()
        assert ret_data["loan_id"] == loan_id
        assert "fine" in ret_data

        # Double return should fail with 409
        dup_res = client.post("/return", json={"loan_id": loan_id})
        assert dup_res.status_code == 409
        assert "already returned" in dup_res.json()["detail"]

    def test_renew_flow(self):
        # Reset renewals and due date for loan 8 to ensure idempotent execution
        db.query("UPDATE loans SET renewals=0, due_on=CURRENT_DATE + 5 WHERE id=8")
        # Loan 8 belongs to Fatima (member 5), due in the future
        res1 = client.post("/loans/8/renew")
        assert res1.status_code == 200
        assert res1.json()["renewals"] == 1

        res2 = client.post("/loans/8/renew")
        assert res2.status_code == 200
        assert res2.json()["renewals"] == 2

        # 3rd renewal reaches limit (max 2 renewals)
        res3 = client.post("/loans/8/renew")
        assert res3.status_code == 409
        assert res3.json()["detail"] == "renewal_limit_reached"

        # Overdue loan cannot be renewed (loan 5 belongs to Priya)
        res_overdue = client.post("/loans/5/renew")
        assert res_overdue.status_code == 409
        assert res_overdue.json()["detail"] == "already_overdue"


class TestFinesPayment:
    def test_pay_fines_flow(self):
        # Ensure member 4 has unpaid fines before testing payment
        db.query("UPDATE fines SET paid=FALSE, paid_at=NULL WHERE member_id=4")
        # Member 4 (Rahul) owes unpaid fines
        res = client.post("/members/4/fines/pay")
        assert res.status_code == 200
        data = res.json()
        assert data["member_id"] == 4
        assert data["cleared"] >= 1
        assert data["paid"] > 0

        # Paying again when no fines remain returns 409
        res_again = client.post("/members/4/fines/pay")
        assert res_again.status_code == 409
        assert res_again.json()["detail"] == "nothing to pay"
