import csv
import io
import json


HEADER = [
    "run_id",
    "input_sha256",
    "rule_version",
    "kind",
    "source_lines",
    "reason",
    "evidence",
    "parcel",
    "basis",
    "basis_label",
    "basis_value",
    "original_value",
    "verified_value",
    "alternate_reason",
    "alternate_detail",
    "expected",
    "payout_id",
    "formal",
]


def build_archive(run, conclusion):
    """导出与页面同一结论（``store.conclusion`` 的结果），不另行挑选金额。"""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(HEADER)
    meta = run["manifest"]
    conclusion_by_parcel = {str(row.get("parcel", "")): row for row in conclusion["rows"]}

    for row in run["stages"][-1]["rows"]:
        parcel = str(row.get("parcel", ""))
        decided = conclusion_by_parcel.get(parcel, {})
        writer.writerow([
            run["run_id"],
            meta["input_sha256"],
            meta["rule_version"],
            "accepted",
            json.dumps(row.get("_sources", [])),
            row.get("status", ""),
            json.dumps(row, ensure_ascii=False),
            parcel,
            decided.get("basis", ""),
            decided.get("basis_label", ""),
            decided.get("basis_value", ""),
            decided.get("original_declared_value", ""),
            decided.get("verified_declared_value", ""),
            decided.get("alternate_reason", ""),
            decided.get("alternate_detail", ""),
            decided.get("expected", ""),
            decided.get("payout_id", ""),
            "1" if conclusion.get("formal") else "0",
        ])
    for row in run["stages"][-1].get("cumulative_quarantine", []):
        writer.writerow([
            run["run_id"],
            meta["input_sha256"],
            meta["rule_version"],
            "quarantine",
            json.dumps(row.get("_sources", [])),
            row.get("reason", ""),
            json.dumps(row, ensure_ascii=False),
            row.get("parcel", ""),
        ])
    for invoice in conclusion.get("invoices", []):
        is_supplement = invoice.get("kind") == "supplement"
        writer.writerow([
            run["run_id"],
            meta["input_sha256"],
            meta["rule_version"],
            "invoice_correction" if is_supplement else "invoice_original",
            json.dumps([invoice.get("invoice_id")]),
            invoice.get("reason", ""),
            json.dumps(invoice, ensure_ascii=False),
            invoice.get("parcel", ""),
            "adopted" if invoice.get("adopted") else "not_adopted",
            invoice.get("value_source", ""),
            invoice.get("declared_value", ""),
            invoice.get("declared_value", "") if not is_supplement else "",
            invoice.get("declared_value", "") if is_supplement else "",
            invoice.get("reason", ""),
            invoice.get("detail", ""),
            "",
            "",
            "1" if conclusion.get("formal") else "0",
        ])
    return "﻿" + stream.getvalue()
