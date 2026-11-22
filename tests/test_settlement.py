import csv
import io

import pytest

from app import store
from app.pipeline import load_sample
from app import settlement
from app.modules.m1_scans.rules import decode

COLUMNS = (
    "scan_id,parcel,node,event_time,received_time,destination,"
    "promised_hours,declared_value,reported,note"
)


def _csv(parcel="0001", value="200", delivered="2026-11-06T00:00:00Z"):
    return "\n".join([
        COLUMNS,
        f"0010,{parcel},收件,2026-11-02T00:00:00Z,2026-11-02T01:00:00Z,JP,24,{value},0,a",
        f"0011,{parcel},起运,2026-11-02T12:00:00Z,2026-11-02T13:00:00Z,JP,24,{value},0,a",
        f"0012,{parcel},清关,2026-11-03T00:00:00Z,2026-11-03T01:00:00Z,JP,24,{value},0,a",
        f"0013,{parcel},妥投,{delivered},2026-11-06T01:00:00Z,JP,24,{value},0,a",
    ])


@pytest.fixture
def run_id(client):
    return client.post("/api/runs", json={"csv": _csv()}).get_json()["run_id"]


def _add(client, run_id, invoice_id, value, parcel="0001", received="2026-11-10T00:00:00Z"):
    return client.post(
        f"/api/runs/{run_id}/invoices",
        json={
            "invoice_id": invoice_id,
            "parcel": parcel,
            "declared_value": value,
            "received_at": received,
        },
    )


def _row(run_id, parcel="0001"):
    return next(r for r in store.conclusion(run_id)["rows"] if r["parcel"] == parcel)


def test_new_invoice_enters_conclusion_before_lock(client, run_id):
    assert _row(run_id)["basis"] == settlement.SCAN
    assert _row(run_id)["expected"] == "20.00"
    assert _add(client, run_id, "INV-1", "1000").status_code == 200
    row = _row(run_id)
    assert row["basis"] == settlement.INVOICE
    assert row["basis_value"] == "1000.00"
    assert row["expected"] == "48.00"
    assert row["original_declared_value"] == "200.00"
    assert row["alternate_value"] == "200.00"
    assert row["alternate_reason"] == "SUPERSEDED_BY_VERIFIED_INVOICE"


def test_each_parcel_has_exactly_one_payout(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    metrics = store.conclusion(run_id)["metrics"]
    assert metrics["unique_payouts"] is True
    assert metrics["payouts"] == metrics["parcels"]
    ids = [row["payout_id"] for row in store.conclusion(run_id)["rows"]]
    assert len(ids) == len(set(ids))


def test_locked_batch_keeps_original_when_no_correction(client, run_id):
    before = _row(run_id)["expected"]
    version = store.get(run_id)["version"]
    assert client.post(
        f"/api/runs/{run_id}/review", json={"version": version, "decision": "已复核"}
    ).status_code == 200
    # 锁定后才补传：只作证据，不改写正式赔款。
    assert _add(client, run_id, "INV-LATE", "5000").status_code == 200
    after = _row(run_id)
    assert after["expected"] == before
    assert after["basis"] == settlement.SCAN
    assert after["formal"] is True
    assert after["pending_correction"] is True
    assert after["alternate_reason"] == "BATCH_LOCKED_KEEP_FORMAL"
    assert _row(run_id)["payout_id"] == f"{run_id}:0001"


def test_correction_before_lock_is_frozen_into_formal(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    expected = _row(run_id)["expected"]
    version = store.get(run_id)["version"]
    client.post(f"/api/runs/{run_id}/review", json={"version": version, "decision": "已复核"})
    later = _add(client, run_id, "INV-2", "500", received="2026-11-12T00:00:00Z")
    assert later.status_code == 200
    row = _row(run_id)
    assert row["basis"] == settlement.INVOICE
    assert row["basis_value"] == "1000.00"
    assert row["expected"] == expected
    assert row["pending_correction"] is True


def test_switch_locked_batch_rejected_and_old_conclusion_kept(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    version = store.get(run_id)["version"]
    client.post(f"/api/runs/{run_id}/review", json={"version": version, "decision": "已复核"})
    before = store.conclusion(run_id)
    current = store.get(run_id)["version"]
    response = client.post(
        f"/api/runs/{run_id}/settlement/switch",
        json={"version": current, "parcel": "0001", "invoice_id": "INV-1"},
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "BATCH_LOCKED"
    assert store.get(run_id)["version"] == current
    after = store.conclusion(run_id)
    assert after["rows"] == before["rows"]
    assert after["metrics"] == before["metrics"]


def test_failed_switch_keeps_previous_conclusion(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    before = store.conclusion(run_id)
    version = store.get(run_id)["version"]
    for body in (
        {"version": version, "parcel": "0001", "invoice_id": "NOPE"},
        {"version": version, "parcel": "9999", "invoice_id": "INV-1"},
        {"version": 999, "parcel": "0001", "invoice_id": "INV-1"},
    ):
        response = client.post(f"/api/runs/{run_id}/settlement/switch", json=body)
        assert response.status_code in (409, 422)
        assert store.get(run_id)["version"] == version
        assert store.conclusion(run_id)["rows"] == before["rows"]


def test_switch_to_original_and_back(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    version = store.get(run_id)["version"]
    assert client.post(
        f"/api/runs/{run_id}/settlement/switch",
        json={"version": version, "parcel": "0001"},
    ).status_code == 200
    row = _row(run_id)
    assert row["basis"] == settlement.SCAN and row["expected"] == "20.00"
    assert row["alternate_reason"] == "ORIGINAL_CHOSEN"
    version = store.get(run_id)["version"]
    assert client.post(
        f"/api/runs/{run_id}/settlement/switch",
        json={"version": version, "parcel": "0001", "invoice_id": "INV-1"},
    ).status_code == 200
    assert _row(run_id)["basis"] == settlement.INVOICE


def test_invoice_retry_is_idempotent(client, run_id):
    first = _add(client, run_id, "INV-1", "1000").get_json()
    total = store.conclusion(run_id)["metrics"]["expected_total"]
    version = store.get(run_id)["version"]
    for _ in range(3):
        again = _add(client, run_id, "INV-1", "1000").get_json()
        assert again["created"] is False
        assert again["invoice_id"] == first["invoice_id"]
    assert len(store.invoices(run_id)) == 1
    assert store.conclusion(run_id)["metrics"]["expected_total"] == total
    assert store.get(run_id)["version"] == version


def test_same_invoice_id_different_value_conflicts(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    response = _add(client, run_id, "INV-1", "900")
    assert response.status_code == 409
    assert len(store.invoices(run_id)) == 1


def test_second_parcel_payout_unchanged_by_other_correction(client, run_id):
    second = client.post("/api/runs", json={"csv": _csv(parcel="0002")}).get_json()["run_id"]
    _add(client, run_id, "INV-1", "1000")
    _add(client, second, "INV-9", "1000", parcel="0002")
    assert _row(run_id)["basis_value"] == "1000.00"
    assert _row(second, "0002")["basis_value"] == "1000.00"


def test_archive_reads_same_conclusion(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    conclusion = store.conclusion(run_id)
    response = client.get(f"/api/runs/{run_id}/archive")
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    header = rows[0]
    assert header[1] == "input_sha256"
    accepted = [row for row in rows[1:] if row[3] == "accepted"]
    by_parcel = {row[header.index("parcel")]: row for row in accepted}
    decided = by_parcel["0001"]
    assert decided[header.index("basis")] == conclusion["rows"][0]["basis"]
    assert decided[header.index("basis_value")] == conclusion["rows"][0]["basis_value"]
    assert decided[header.index("expected")] == conclusion["rows"][0]["expected"]
    kinds = {row[3] for row in rows[1:]}
    assert "invoice_original" in kinds and "invoice_correction" in kinds


def test_archive_after_lock_keeps_formal_amount(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    version = store.get(run_id)["version"]
    client.post(f"/api/runs/{run_id}/review", json={"version": version, "decision": "已复核"})
    _add(client, run_id, "INV-2", "5000", received="2026-11-12T00:00:00Z")
    conclusion = store.conclusion(run_id)
    rows = list(csv.reader(io.StringIO(
        client.get(f"/api/runs/{run_id}/archive").data.decode("utf-8-sig")
    )))
    header = rows[0]
    accepted = {row[header.index("parcel")]: row for row in rows[1:] if row[3] == "accepted"}
    assert accepted["0001"][header.index("expected")] == conclusion["rows"][0]["expected"]
    assert accepted["0001"][header.index("formal")] == "1"


def test_original_scan_and_both_invoices_retained(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    evidence = store.conclusion(run_id)["invoices"]
    kinds = [item["kind"] for item in evidence]
    assert kinds.count("original") == 1
    assert kinds.count("supplement") == 1
    original = next(item for item in evidence if item["kind"] == "original")
    assert original["declared_value"] == "200.00"
    assert original["adopted"] is False  # 核验金额被采用，原申报仍保留
    assert decode(load_sample("dirty.csv"))[1] == "utf-8-sig"


def test_page_shows_basis_and_alternate_reason(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    page = client.get("/compensation")
    assert page.status_code == 200
    body = page.data.decode("utf-8")
    assert "保价依据" in body
    assert "未采用" in body
    assert "1000.00" in body


def test_invoice_rejects_unknown_parcel(client, run_id):
    response = _add(client, run_id, "INV-X", "100", parcel="8888")
    assert response.status_code == 422
    assert response.get_json()["code"] == "UNKNOWN_PARCEL"
    assert store.invoices(run_id) == []


def test_unlock_restores_live_correction(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    version = store.get(run_id)["version"]
    client.post(f"/api/runs/{run_id}/review", json={"version": version, "decision": "已复核"})
    _add(client, run_id, "INV-2", "5000", received="2026-11-12T00:00:00Z")
    assert _row(run_id)["basis_value"] == "1000.00"
    version = store.get(run_id)["version"]
    assert client.post(
        f"/api/runs/{run_id}/review", json={"version": version, "decision": "退回复核"}
    ).status_code == 200
    # 退回后按最新核验金额重新裁决。
    row = _row(run_id)
    assert row["basis_value"] == "5000.00"
    assert row["formal"] is False


def test_relock_cannot_pull_late_invoice_into_formal(client, run_id):
    version = store.get(run_id)["version"]
    client.post(f"/api/runs/{run_id}/review", json={"version": version, "decision": "已复核"})
    _add(client, run_id, "INV-LATE", "5000")
    formal_before = _row(run_id)["expected"]
    current = store.get(run_id)["version"]
    response = client.post(
        f"/api/runs/{run_id}/review", json={"version": current, "decision": "已复核"}
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "ALREADY_LOCKED"
    assert _row(run_id)["expected"] == formal_before
    assert _row(run_id)["basis_value"] == "200.00"


def test_archive_unchanged_after_failed_switch(client, run_id):
    _add(client, run_id, "INV-1", "1000")
    version = store.get(run_id)["version"]
    client.post(f"/api/runs/{run_id}/review", json={"version": version, "decision": "已复核"})

    def snapshot():
        data = client.get(f"/api/runs/{run_id}/archive").data
        return data

    before = snapshot()
    current = store.get(run_id)["version"]
    client.post(
        f"/api/runs/{run_id}/settlement/switch",
        json={"version": current, "parcel": "0001", "invoice_id": "ORIG-0001"},
    )
    assert snapshot() == before


def test_dirty_sample_still_balanced_with_no_invoices(client):
    p = client.post("/api/runs", json={"sample": "dirty.csv"}).get_json()
    conclusion = store.conclusion(p["run_id"])
    assert conclusion["metrics"]["unique_payouts"] is True
    assert conclusion["metrics"]["basis_invoice"] == 0
    assert p["stages"][5]["metrics"]["balanced"] is True
