import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app.config import DATA_DIR
from app.errors import DomainError
from app.values import instant, money, number
from app import settlement


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "records.db", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,"
        "version INTEGER NOT NULL DEFAULT 1,decision TEXT NOT NULL DEFAULT '待复核',"
        "locked_at TEXT,formal TEXT)"
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    if "locked_at" not in columns:
        db.execute("ALTER TABLE runs ADD COLUMN locked_at TEXT")
    if "formal" not in columns:
        db.execute("ALTER TABLE runs ADD COLUMN formal TEXT")
    db.execute(
        "CREATE TABLE IF NOT EXISTS invoices (run_id TEXT NOT NULL,invoice_id TEXT NOT NULL,"
        "parcel TEXT NOT NULL,kind TEXT NOT NULL,declared_value TEXT NOT NULL,"
        "received_at TEXT NOT NULL,fingerprint TEXT NOT NULL,note TEXT,created_at TEXT NOT NULL,"
        "PRIMARY KEY(run_id,invoice_id),UNIQUE(run_id,fingerprint))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS settlement_choices (run_id TEXT NOT NULL,parcel TEXT NOT NULL,"
        "invoice_id TEXT NOT NULL,PRIMARY KEY(run_id,parcel))"
    )
    return db


def _run_row(db, run_id):
    return db.execute(
        "SELECT payload,version,decision,locked_at,formal FROM runs WHERE id=?", (run_id,)
    ).fetchone()


def save(run_id, payload):
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO runs(id,payload) VALUES (?,?)",
            (run_id, json.dumps(payload, ensure_ascii=False)),
        )
    return get(run_id)


def get(run_id):
    with connect() as db:
        row = _run_row(db, run_id)
    if not row:
        raise DomainError("NOT_FOUND", "批次不存在", 404)
    return {
        **json.loads(row[0]),
        "run_id": run_id,
        "version": row[1],
        "decision": row[2],
        "locked_at": row[3],
        "locked": row[2] == "已复核" and row[4] is not None,
    }


def latest():
    with connect() as db:
        row = db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return get(row[0]) if row else None


def history():
    with connect() as db:
        return [
            {"run_id": i, "version": v, "decision": d, "locked": d == "已复核" and f is not None}
            for i, v, d, f in db.execute(
                "SELECT id,version,decision,formal FROM runs ORDER BY rowid DESC"
            )
        ]


# ---------------------------------------------------------------- invoices

def _fingerprint(run_id, invoice_id, parcel, value):
    body = json.dumps(
        [run_id, invoice_id, parcel, value], ensure_ascii=False, sort_keys=True
    ).encode()
    return hashlib.sha256(body).hexdigest()


_INVOICE_COLUMNS = (
    "run_id,invoice_id,parcel,kind,declared_value,received_at,note,created_at"
)


def _invoice_row(row):
    return {
        "run_id": row[0],
        "invoice_id": row[1],
        "parcel": row[2],
        "kind": row[3],
        "declared_value": row[4],
        "received_at": row[5],
        "note": row[6],
        "created_at": row[7],
    }


def _fetch_invoice(db, run_id, invoice_id):
    row = db.execute(
        f"SELECT {_INVOICE_COLUMNS} FROM invoices WHERE run_id=? AND invoice_id=?",
        (run_id, invoice_id),
    ).fetchone()
    return _invoice_row(row) if row else None


def invoices(run_id):
    with connect() as db:
        if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
            raise DomainError("NOT_FOUND", "批次不存在", 404)
        rows = db.execute(
            f"SELECT {_INVOICE_COLUMNS} FROM invoices WHERE run_id=? "
            "ORDER BY received_at,invoice_id",
            (run_id,),
        ).fetchall()
    return [_invoice_row(row) for row in rows]


def _batch_parcels(db, run_id):
    payload = json.loads(
        db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    )
    stages = payload.get("stages", [])
    rows = stages[5].get("rows", []) if len(stages) > 5 else []
    return {str(row.get("parcel", "")) for row in rows}


def add_invoice(run_id, payload):
    """补传一张商业发票。同一补正重试幂等，不产生新赔款或新记录。"""
    if not isinstance(payload, dict):
        raise DomainError("BAD_INVOICE", "发票需要对象")
    invoice_id = str(payload.get("invoice_id") or "").strip()
    parcel = str(payload.get("parcel") or "").strip()
    if not invoice_id:
        raise DomainError("BAD_INVOICE", "缺少发票号")
    if not parcel:
        raise DomainError("BAD_INVOICE", "缺少包裹号")
    try:
        declared = number(payload.get("declared_value"))
    except DomainError as error:
        raise DomainError("BAD_INVOICE", f"保价金额无效：{error.message}")
    if declared < 0:
        raise DomainError("BAD_INVOICE", "保价金额不能为负")
    value = money(declared)
    try:
        received_at = (
            instant(payload["received_at"]).isoformat()
            if payload.get("received_at") is not None
            else _utcnow()
        )
    except DomainError as error:
        raise DomainError("BAD_INVOICE", f"核验时间无效：{error.message}")
    fingerprint = _fingerprint(run_id, invoice_id, parcel, value)

    with connect() as db:
        if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
            raise DomainError("NOT_FOUND", "批次不存在", 404)
        if parcel not in _batch_parcels(db, run_id):
            raise DomainError("UNKNOWN_PARCEL", "包裹不在本批次", 422)
        known = _fetch_invoice(db, run_id, invoice_id)
        if known:
            if known["declared_value"] != value or known["parcel"] != parcel:
                raise DomainError("INVOICE_CONFLICT", "同一发票号的核验金额不一致", 409)
            return {"created": False, **known}
        duplicate = db.execute(
            "SELECT invoice_id FROM invoices WHERE run_id=? AND fingerprint=?",
            (run_id, fingerprint),
        ).fetchone()
        if duplicate:
            return {"created": False, **_fetch_invoice(db, run_id, duplicate[0])}
        db.execute(
            f"INSERT INTO invoices({_INVOICE_COLUMNS},fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, invoice_id, parcel, "supplement", value, received_at,
             payload.get("note"), _utcnow(), fingerprint),
        )
        # 新核验金额成为“最新”，撤销该票的显式切换选择。
        db.execute(
            "DELETE FROM settlement_choices WHERE run_id=? AND parcel=?", (run_id, parcel)
        )
        # 结论可能改变，推进版本，使旧版本的复核/切换请求失效。
        db.execute("UPDATE runs SET version=version+1 WHERE id=?", (run_id,))
        created = _fetch_invoice(db, run_id, invoice_id)
    return {"created": True, **created}


def choices(run_id):
    with connect() as db:
        return {
            parcel: invoice_id
            for parcel, invoice_id in db.execute(
                "SELECT parcel,invoice_id FROM settlement_choices WHERE run_id=?", (run_id,)
            )
        }


# --------------------------------------------------------------- conclusion

def _read_conclusion(db, run_id):
    row = _run_row(db, run_id)
    if not row:
        raise DomainError("NOT_FOUND", "批次不存在", 404)
    payload, version, decision, locked_at, formal = row
    evidence = [
        _invoice_row(item)
        for item in db.execute(
            f"SELECT {_INVOICE_COLUMNS} FROM invoices WHERE run_id=? "
            "ORDER BY received_at,invoice_id",
            (run_id,),
        ).fetchall()
    ]
    choice_map = {
        parcel: invoice_id
        for parcel, invoice_id in db.execute(
            "SELECT parcel,invoice_id FROM settlement_choices WHERE run_id=?", (run_id,)
        ).fetchall()
    }
    run = {
        **json.loads(payload),
        "run_id": run_id,
        "version": version,
        "decision": decision,
        "locked_at": locked_at,
    }
    if decision == "已复核" and formal:
        result = settlement.present(run, evidence, json.loads(formal))
    else:
        result = settlement.compute(run, evidence, choices=choice_map)
    result["version"] = version
    result["decision"] = decision
    return result


def conclusion(run_id):
    """读取该批次唯一生效的赔付结论；页面、接口与证据导出共用此入口。"""
    with connect() as db:
        return _read_conclusion(db, run_id)


def _freeze(db, run_id, payload, version, decision):
    evidence = [
        _invoice_row(item)
        for item in db.execute(
            f"SELECT {_INVOICE_COLUMNS} FROM invoices WHERE run_id=? "
            "ORDER BY received_at,invoice_id",
            (run_id,),
        ).fetchall()
    ]
    choice_map = {
        parcel: invoice_id
        for parcel, invoice_id in db.execute(
            "SELECT parcel,invoice_id FROM settlement_choices WHERE run_id=?", (run_id,)
        ).fetchall()
    }
    run = {**payload, "run_id": run_id, "version": version, "decision": decision}
    return settlement.compute(
        run, evidence, choices=choice_map, formal=True, locked_at=_utcnow()
    )


def review(run_id, version, decision):
    """复核锁定：冻结当前结论为正式版本；退回解除冻结。旧版结论在失败时保持。"""
    if decision not in {"已复核", "退回复核"}:
        raise DomainError("BAD_DECISION", "复核状态无效")
    with connect() as db:
        row = _run_row(db, run_id)
        if not row:
            raise DomainError("NOT_FOUND", "批次不存在", 404)
        if row[1] != version:
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
        if decision == "已复核":
            if row[4] is not None:
                # 已锁定批次不得重新冻结，否则锁定后补传会倒灌进正式结论。
                raise DomainError("ALREADY_LOCKED", "批次已锁定，如需改判请先退回", 409)
            formal = _freeze(db, run_id, json.loads(row[0]), row[1], decision)
            cursor = db.execute(
                "UPDATE runs SET version=version+1,decision=?,locked_at=?,formal=? "
                "WHERE id=? AND version=?",
                (decision, formal["locked_at"],
                 json.dumps(formal, ensure_ascii=False), run_id, version),
            )
        else:
            cursor = db.execute(
                "UPDATE runs SET version=version+1,decision=?,locked_at=NULL,formal=NULL "
                "WHERE id=? AND version=?",
                (decision, run_id, version),
            )
        if cursor.rowcount != 1:
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
    return get(run_id)


# ------------------------------------------------------------------- switch

def switch(run_id, version, parcel=None, invoice_id=None):
    """锁定前切换某票采用的保价依据；失败时旧版结论与正式结论保持原样。

    ``parcel`` 必填；``invoice_id`` 为空表示该票回退到原申报金额。所有校验
    与写入在同一事务内完成，任何失败都不落库、不改变版本。
    """
    if not parcel:
        raise DomainError("BAD_PARCEL", "请提供包裹号")
    parcel = str(parcel)
    with connect() as db:
        row = _run_row(db, run_id)
        if not row:
            raise DomainError("NOT_FOUND", "批次不存在", 404)
        if row[2] == "已复核" and row[4]:
            raise DomainError("BATCH_LOCKED", "批次已锁定，正式结论沿用锁定前金额", 409)
        if row[1] != version:
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
        if parcel not in _batch_parcels(db, run_id):
            raise DomainError("UNKNOWN_PARCEL", "包裹不在本批次", 422)
        if invoice_id is not None:
            invoice = _fetch_invoice(db, run_id, str(invoice_id))
            if not invoice:
                raise DomainError("UNKNOWN_INVOICE", "发票不存在", 422)
            if invoice["parcel"] != parcel:
                raise DomainError("INVOICE_PARCEL_MISMATCH", "发票不属于该包裹", 422)
            db.execute(
                "INSERT INTO settlement_choices(run_id,parcel,invoice_id) VALUES (?,?,?) "
                "ON CONFLICT(run_id,parcel) DO UPDATE SET invoice_id=excluded.invoice_id",
                (run_id, parcel, str(invoice_id)),
            )
        else:
            # 人工选定沿用原申报金额。
            db.execute(
                "INSERT INTO settlement_choices(run_id,parcel,invoice_id) VALUES (?,?,?) "
                "ON CONFLICT(run_id,parcel) DO UPDATE SET invoice_id=excluded.invoice_id",
                (run_id, parcel, settlement.ORIGINAL),
            )
        cursor = db.execute(
            "UPDATE runs SET version=version+1 WHERE id=? AND version=?", (run_id, version)
        )
        if cursor.rowcount != 1:
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
        # 在同一事务内用切换后的数据重算，失败则整体回滚。
        return _read_conclusion(db, run_id)
