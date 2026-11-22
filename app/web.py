from flask import render_template
from app.config import MODULES, TITLE, SHORT
from app.pipeline import execute, load_sample
from app import store


COLUMNS = [
    ['scan_id', 'parcel', 'node', 'event_time', 'received_time'],
    ['scan_id', 'parcel', 'node', 'destination', 'zone'],
    ['scan_id', 'parcel', 'event_utc', 'received_utc', 'upload_lag_hours'],
    ['parcel', 'accepted_at', 'delivered_at', 'delivery_uploaded_at', 'scan_count', 'path'],
    ['parcel', 'accepted_at', 'delivered_at', 'working_hours', 'promised_hours',
     'late_hours', 'expected', 'sla'],
    ['parcel', 'basis_label', 'basis_value', 'alternate_value', 'alternate_detail',
     'expected', 'reported', 'difference', 'status'],
    ['parcel', 'delivered_at', 'sla', 'expected', 'status'],
]

LABELS = {
    'scan_id': '扫描号', 'parcel': '包裹号', 'node': '节点', 'event_time': '发生时间',
    'received_time': '上传时间', 'destination': '目的地', 'zone': '时区',
    'event_utc': '发生 UTC', 'received_utc': '上传 UTC', 'upload_lag_hours': '补传时差 / h',
    'accepted_at': '收件 UTC', 'delivered_at': '妥投 UTC',
    'delivery_uploaded_at': '妥投上传 UTC', 'scan_count': '扫描数', 'path': '节点路径',
    'working_hours': '计时 / h', 'promised_hours': '承诺 / h', 'late_hours': '超时 / h',
    'expected': '赔付 / 元', 'sla': '时效结论', 'reported': '申报 / 元',
    'difference': '差额 / 元', 'status': '对账结论', 'accepted': '采纳扫描',
    'late_uploads': '迟到上传', 'parcels': '包裹数', 'late_parcels': '超时包裹',
    'compensation': '赔付合计', 'delivered': '妥投数', 'path_errors': '路径异常',
    'records': '归档数', 'source_lines': '源行数', 'sha256': '摘要', 'ready': '可归档',
    'differences': '差异数', 'basis_label': '保价依据', 'basis_value': '依据金额 / 元',
    'alternate_value': '未采用金额 / 元', 'alternate_detail': '未采用原因',
    'basis_scan': '原申报票数', 'basis_invoice': '核验票数', 'corrections': '补传发票',
    'adopted_corrections': '采用核验', 'pending_corrections': '锁定后待补',
    'payouts': '赔款笔数', 'unique_payouts': '每票唯一', 'formal': '正式结论',
    'difference_total': '差额合计', 'locked_at': '锁定时刻',
}

EXTRA_LABELS = {
    'accepted_inputs': '采纳来源行', 'quarantine_inputs': '隔离来源行',
    'output_records': '结果记录', 'input': '输入行', 'excluded': '排除行',
    'balanced': '行数平衡', 'expected_total': '预期合计', 'reported_total': '申报合计',
    'differences': '差异项', 'blank': '空行', 'repeated_headers': '分段表头',
    'encoding': '编码', 'duplicate': '重复行', 'quarantine': '隔离行',
    'accepted': '采纳行', 'sha256': '校验摘要', 'ready': '可归档',
}


def register_pages(app):
    def page(index):
        run = store.latest() or execute(load_sample("dirty.csv"))
        conclusion = store.conclusion(run["run_id"])
        decided = {str(row.get("parcel", "")): row for row in conclusion["rows"]}
        stage = run["stages"][index]
        rows = (
            conclusion["rows"] if index == 5
            else [
                {**row, **{k: v for k, v in decided.get(str(row.get("parcel", "")), {}).items()
                           if k in ('basis_label', 'basis_value', 'alternate_value',
                                    'alternate_detail', 'payout_id', 'formal')}}
                for row in stage["rows"]
            ]
        )
        labels = dict(LABELS)
        for key, label in EXTRA_LABELS.items():
            labels.setdefault(key, label)
        return render_template(
            f"m{index + 1}.html", title=TITLE, short=SHORT, modules=MODULES,
            index=index, run=run, stage=stage, rows=rows, conclusion=conclusion,
            columns=COLUMNS[index][:12], history=store.history(), labels=labels,
        )

    app.add_url_rule("/", view_func=lambda: page(4))
    for index, (slug, name) in enumerate(MODULES):
        app.add_url_rule("/" + slug, endpoint="page_" + slug,
                         view_func=lambda index=index: page(index))
