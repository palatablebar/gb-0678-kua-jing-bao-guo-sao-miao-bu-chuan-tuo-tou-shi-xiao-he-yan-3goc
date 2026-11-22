"""保价依据裁决（每票唯一赔款）。

同一票最多有两份保价金额：

* **原申报金额**：原始扫描 ``declared_value`` 派生出的原申报发票，永远保留；
* **核验金额**：补传商业发票（supplement）所载的最新核验金额。

裁决时点
--------
补正金额只在批次被复核锁定（``decision == 已复核``）**之前**进入正式结论；
锁定瞬间把当时的结论冻结为正式版本。锁定之后补传的发票继续作为证据保留，
但不再改写赔款。因此：

* 未锁定批次：显示当前唯一结论（默认采用最新核验金额），状态为预核；
* 锁定批次：正式结论永久冻结，沿用锁定前的金额；锁定前没有补正则沿用原
  申报金额，锁定后补传只作 ``pending_correction`` 展示；
* 任一时刻每票只有一笔 ``payout_id = run_id:parcel`` 的赔款。

页面、接口与证据导出都读取本模块给出的同一份结论，不得各自重新挑选金额。
"""

from copy import deepcopy
from decimal import Decimal

from app.errors import DomainError
from app.values import instant, money, number

SCAN = "scan"
INVOICE = "invoice"

# 显式选择“沿用原申报金额”时写入 settlement_choices 的哨兵值。
ORIGINAL = "__original__"

BASIS_LABELS = {SCAN: "原申报保价", INVOICE: "补传发票核验保价"}

# 代码 -> 中文说明
REASONS = {
    "SUPERSEDED": ("SUPERSEDED_BY_VERIFIED_INVOICE", "原申报金额已被最新核验发票取代"),
    "SAME": ("SAME_AS_ORIGINAL", "核验金额与原申报一致，无需切换依据"),
    "NO_CORRECTION": ("NO_CORRECTION", "没有补传发票，沿用原申报金额"),
    "OLDER": ("OLDER_CORRECTION", "存在时间更晚的核验发票"),
    "ADOPTED": ("ADOPTED_VERIFIED_INVOICE", "采用该最新核验金额作为正式依据"),
    "ORIGINAL_ADOPTED": ("ORIGINAL_DECLARATION_ADOPTED", "原申报金额作为正式依据"),
    "MANUAL_ORIGINAL": ("ORIGINAL_CHOSEN", "人工选定沿用原申报金额"),
    "NOT_CHOSEN": ("NOT_CHOSEN_BY_OPERATOR", "人工选定其他保价依据，该金额未采用"),
    "LOCKED": ("BATCH_LOCKED_KEEP_FORMAL", "批次已锁定，正式结论沿用锁定前金额"),
    "EVIDENCE_ONLY": ("EVIDENCE_ONLY", "批次已锁定，仅作证据保留，不改写赔款"),
    "UNMATCHED": ("UNMATCHED_PARCEL", "该发票对应的包裹不在本批次"),
}


def _reason(key):
    code, detail = REASONS[key]
    return {"alternate_reason": code, "alternate_detail": detail}


def _ordered(invoices):
    return sorted(
        invoices,
        key=lambda item: (instant(item["received_at"]), str(item.get("invoice_id", ""))),
    )


def supplements_by_parcel(invoices):
    grouped = {}
    for invoice in invoices or []:
        if invoice.get("kind", "supplement") != "supplement":
            continue
        grouped.setdefault(str(invoice.get("parcel", "")), []).append(invoice)
    return {parcel: _ordered(items) for parcel, items in grouped.items()}


def original_invoice(run_id, row):
    """原始扫描派生的原申报发票；不可删除，永远作为比对证据。"""
    parcel = str(row.get("parcel", ""))
    return {
        "invoice_id": f"ORIG-{parcel}",
        "run_id": run_id,
        "parcel": parcel,
        "kind": "original",
        "declared_value": money(row.get("original_declared_value", "0")),
        "received_at": str(row.get("_original_received_at") or ""),
        "value_source": "原始扫描申报",
    }


def _source_rows(run):
    stages = run.get("stages", [])
    if len(stages) > 5:
        return stages[5].get("rows", [])
    return stages[-1].get("rows", []) if stages else []


def _payout(basis_value, late, rate):
    value = basis_value if isinstance(basis_value, Decimal) else Decimal(str(basis_value))
    return money(min(value * Decimal("0.10"), max(Decimal(0), late) * rate))


def decide_rows(run_id, source_rows, invoices=None, choices=None, rate=None,
                formal=False):
    """对一组对账行做保价裁决，返回 (rows, payouts, basis_counts)。"""
    supplements = supplements_by_parcel(invoices)
    choices = choices or {}
    rate = number("1.00") if rate is None else rate
    rows, payouts = [], set()
    basis_counts = {SCAN: 0, INVOICE: 0}

    for source in source_rows:
        parcel = str(source.get("parcel", ""))
        original = money(source.get("declared_value", "0"))
        candidates = supplements.get(parcel, [])
        known = choices.get(parcel)
        if known == ORIGINAL:
            adopted = None
        elif known:
            adopted = next(
                (c for c in candidates if c.get("invoice_id") == known), None
            )
        else:
            adopted = candidates[-1] if candidates else None
        latest = candidates[-1] if candidates else None
        verified = money(latest["declared_value"]) if latest else None

        if adopted is not None and money(adopted["declared_value"]) != original:
            basis, basis_value = INVOICE, money(adopted["declared_value"])
            basis_invoice_id = adopted.get("invoice_id")
            alternate, reason_key = original, "SUPERSEDED"
        elif adopted is not None:
            basis, basis_value, basis_invoice_id = SCAN, original, None
            alternate = verified
            reason_key = "SAME"
        elif candidates:
            basis, basis_value, basis_invoice_id = SCAN, original, None
            alternate = verified
            reason_key = "MANUAL_ORIGINAL" if known == ORIGINAL else "NO_CORRECTION"
        else:
            basis, basis_value, basis_invoice_id = SCAN, original, None
            alternate, reason_key = None, "NO_CORRECTION"

        late = max(Decimal(0), number(source.get("late_hours", "0")))
        expected = _payout(basis_value, late, rate)
        expected_decimal = Decimal(str(expected))
        reported = number(source.get("reported", "0"))
        payout_id = f"{run_id}:{parcel}"
        if payout_id in payouts:
            raise DomainError("DUPLICATE_PAYOUT", "同一票出现两笔赔款", 409)
        payouts.add(payout_id)
        basis_counts[basis] += 1

        rows.append({
            **source,
            "declared_value": basis_value,
            "original_declared_value": original,
            "verified_declared_value": verified,
            "basis": basis,
            "basis_label": BASIS_LABELS[basis],
            "basis_value": basis_value,
            "basis_invoice_id": basis_invoice_id,
            "alternate_value": alternate,
            **_reason(reason_key),
            "expected": expected,
            "difference": str(expected_decimal - reported),
            "status": "通过" if expected == reported else "差异",
            "payout_id": payout_id,
            "formal": formal,
            "pending_correction": False,
        })
    return rows, payouts, basis_counts


def compute(run, invoices=None, choices=None, formal=False, locked_at=None):
    """计算批次当前唯一生效的赔付结论；锁定时以此冻结为正式版本。"""
    rate = number(run.get("parameters", {}).get("hourly_compensation", "1.00"))
    rows, payouts, basis_counts = decide_rows(
        run.get("run_id", ""), _source_rows(run), invoices, choices, rate, formal
    )
    return {
        "run_id": run.get("run_id", ""),
        "locked": formal,
        "formal": formal,
        "locked_at": locked_at,
        "rows": rows,
        "invoices": evidence(run.get("run_id", ""), invoices, rows, locked=formal),
        "metrics": _metrics(rows, payouts, basis_counts, invoices, formal),
    }


def present(run, invoices, frozen):
    """锁定批次：正式结论取自冻结版本，仅补记锁定后到达的证据。"""
    result = deepcopy(frozen)
    result["run_id"] = run.get("run_id", "")
    result["locked"] = True
    result["formal"] = True
    result["locked_at"] = frozen.get("locked_at") or run.get("locked_at")
    locked_at = result["locked_at"]
    supplements = supplements_by_parcel(invoices)

    for row in result.get("rows", []):
        row["formal"] = True
        row["pending_correction"] = False
        candidates = supplements.get(str(row.get("parcel", "")), [])
        latest = candidates[-1] if candidates else None
        row["verified_declared_value"] = money(latest["declared_value"]) if latest else None
        late_evidence = [
            item for item in candidates
            if locked_at and str(item.get("created_at", "")) > str(locked_at)
            and money(item["declared_value"]) != row["basis_value"]
        ]
        if late_evidence:
            row["pending_correction"] = True
            row["alternate_value"] = money(late_evidence[-1]["declared_value"])
            row.update(_reason("LOCKED"))

    result["invoices"] = evidence(
        result["run_id"], invoices, result.get("rows", []), locked=True
    )
    basis_counts = {
        SCAN: sum(1 for row in result["rows"] if row.get("basis") == SCAN),
        INVOICE: sum(1 for row in result["rows"] if row.get("basis") == INVOICE),
    }
    payouts = {row.get("payout_id") for row in result["rows"]}
    result["metrics"] = _metrics(result["rows"], payouts, basis_counts, invoices, True)
    return result


def _metrics(rows, payouts, basis_counts, invoices, formal):
    expected_total = sum((Decimal(str(row["expected"])) for row in rows), Decimal(0))
    reported_total = sum(
        (Decimal(str(row.get("reported", "0"))) for row in rows), Decimal(0)
    )
    supplements = [i for i in invoices or [] if i.get("kind", "supplement") == "supplement"]
    return {
        "parcels": len(rows),
        "payouts": len(payouts),
        "unique_payouts": len(payouts) == len(rows),
        "basis_scan": basis_counts[SCAN],
        "basis_invoice": basis_counts[INVOICE],
        "corrections": len(supplements),
        "adopted_corrections": sum(1 for row in rows if row.get("basis") == INVOICE),
        "pending_corrections": sum(1 for row in rows if row.get("pending_correction")),
        "expected_total": money(expected_total),
        "reported_total": money(reported_total),
        "difference_total": money(expected_total - reported_total),
        "differences": sum(1 for row in rows if row.get("status") == "差异"),
        "formal": formal,
    }


def evidence(run_id, invoices, rows, locked):
    """逐张列出原始发票与补传发票，标明是否采用及原因。"""
    by_parcel = {str(row.get("parcel", "")): row for row in rows}
    adopted_ids = {
        (str(row.get("parcel", "")), row.get("basis_invoice_id"))
        for row in rows
        if row.get("basis") == INVOICE and row.get("basis_invoice_id")
    }
    supplements = supplements_by_parcel(invoices)
    evidence_rows = []

    for row in rows:
        adopted = row.get("basis") == SCAN
        if adopted and row.get("alternate_reason") == "ORIGINAL_CHOSEN":
            code, detail = REASONS["MANUAL_ORIGINAL"]
        elif adopted:
            code, detail = REASONS["ORIGINAL_ADOPTED"]
        else:
            code, detail = REASONS["SUPERSEDED"]
        evidence_rows.append({
            **original_invoice(run_id, row),
            "adopted": adopted,
            "reason": code,
            "detail": detail,
        })

    for invoice in invoices or []:
        if invoice.get("kind", "supplement") != "supplement":
            continue
        parcel = str(invoice.get("parcel", ""))
        row = by_parcel.get(parcel)
        if row is None:
            code, detail = REASONS["UNMATCHED"]
            adopted = False
        elif (parcel, invoice.get("invoice_id")) in adopted_ids:
            code, detail = REASONS["ADOPTED"]
            adopted = True
        elif row.get("alternate_reason") == "ORIGINAL_CHOSEN":
            code, detail = REASONS["NOT_CHOSEN"]
            adopted = False
        elif locked:
            code, detail = REASONS["EVIDENCE_ONLY"]
            adopted = False
        else:
            candidates = supplements.get(parcel, [])
            latest = candidates[-1] if candidates else None
            if latest is not None and latest.get("invoice_id") != invoice.get("invoice_id"):
                code, detail = REASONS["OLDER"]
            else:
                code, detail = REASONS["SAME"]
            adopted = False
        evidence_rows.append({
            **invoice,
            "adopted": adopted,
            "reason": code,
            "detail": detail,
        })
    return evidence_rows
