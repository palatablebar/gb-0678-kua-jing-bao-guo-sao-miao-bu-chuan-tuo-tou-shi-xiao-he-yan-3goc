# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## 保价依据与赔款

`GET /api/runs/<id>/conclusion` 返回该批次唯一生效的赔款结论，页面与证据导出
都读取这一份结果：

- `rows[]`：每票一行，含 `basis`（scan/invoice）、`basis_label`、`basis_value`、
  `original_declared_value`、`verified_declared_value`、`alternate_value`、
  `alternate_reason`、`alternate_detail`、`expected`、`payout_id`、`formal`、
  `pending_correction`。`payout_id` 固定为 `run_id:parcel`，每票只有一笔。
- `invoices[]`：原始申报发票（kind=original）与每张补传发票（kind=supplement），
  逐张标注 `adopted` 及未采用原因。
- `metrics`：`payouts`、`unique_payouts`、`basis_scan`、`basis_invoice`、
  `corrections`、`pending_corrections`、`expected_total` 等。

`POST /api/runs/<id>/invoices` 补传商业发票，请求体含
`invoice_id`、`parcel`、`declared_value`，可选 `received_at`、`note`。
同一补正重试幂等（按发票号+金额指纹），返回 `created=false`，不新增赔款；
同号不同额返回 409 `INVOICE_CONFLICT`，包裹不在本批返回 422 `UNKNOWN_PARCEL`。

`POST /api/runs/<id>/settlement/switch` 在锁定前切换某票依据，请求体含
`version`、`parcel`、`invoice_id`（省略表示回退到原申报金额）。校验与写入同事务，
任何失败都不落库：版本过期 409 `STALE_VERSION`，已锁定 409 `BATCH_LOCKED`，
发票不存在/不属于该票 422。失败后旧版结论与已导出证据保持原样。

`POST /api/runs/<id>/review` 的 `已复核` 会冻结当前结论为正式版本；锁定后补传
只作证据并标记 `pending_correction`，不改写赔款。已锁定批次再次提交 `已复核`
返回 409 `ALREADY_LOCKED`，防止锁定后补正倒灌正式结论；`退回复核` 解除冻结并
按最新核验金额重算。
