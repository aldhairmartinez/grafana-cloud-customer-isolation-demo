# Isolation validation checklist

Run this **in each customer user's own browser session**, signed in as that user. Admin queries that include a customer filter are **not** evidence of isolation, and neither is `grafana/isolation.py verify`; both only show the configuration.

Before testing:
- [ ] `python3 grafana/isolation.py verify` passes.
- [ ] The tester's stack role is **None**, and they're in exactly one customer team.
- [ ] The tester signed out and back in after their team assignment (or opened a fresh private window).

The examples assume the tester is **Customer A** (`customer-a`) and the "other" customer is **Customer B** (`customer-b`). Swap the values when testing as Customer B, and repeat for every customer.

Record for each check: **pass/fail**, the query, and a screenshot.

## 1. Shared dashboard
- [ ] The dashboard opens from the demo folder. No other folders or dashboards are visible.
- [ ] The **Customer** dropdown lists only `customer-a`.
- [ ] With **All** selected, every panel legend and every log line shows only "Customer A".

## 2. Broad queries (Explore → `customer-isolation-lbac-prom` (default `DS_PROM_UID`))
- [ ] `publishing_http_requests_total` with no filter returns only `customer_id="customer-a"`.
- [ ] `count by (customer_id, customer_name) ({demo="customer-isolation"})` returns only Customer A.
- [ ] `count({__name__=~".+"})` counts only series visible to Customer A. Compare with an operator's result: it should be smaller.

## 3. Other-customer queries
- [ ] Metrics: `{customer_id="customer-b"}` returns **no data**.
- [ ] Metrics: `{customer_name="Customer B"}` returns **no data**.
- [ ] Logs (Explore → `customer-isolation-lbac-logs` (default `DS_LOGS_UID`)): `{customer_id="customer-b"}` returns **no logs**.
- [ ] Logs: `{demo="customer-isolation"} |= "Customer B"` returns **no logs**.
- [ ] Logs: `{demo="customer-isolation"}` with no other filter shows only Customer A.

If a customer name is a short string that appears in other words, use a more specific filter such as `|= "for Customer B"`.

## 4. Label discovery
- [ ] Metrics label browser: the values of `customer_id` and `customer_name` show only Customer A.
- [ ] Logs label browser: the values of `customer_id` and `customer_name` show only Customer A.
- [ ] Drilldown (Metrics and Logs), if enabled: no Customer B values appear in label or service lists.

## 5. Unrestricted data source access
- [ ] The data source picker shows only the two LBAC data sources. The default `grafanacloud-prom`, `grafanacloud-logs` and any others pointing at the same backends are absent.
- [ ] Opening Explore with a URL that targets a default data source fails or shows no data source. To try this, an operator can share an Explore link that uses `grafanacloud-prom`.
- [ ] The user can't open **Connections → Data sources**, data source **Permissions**, or LBAC rules.
- [ ] The user can't open **Administration** pages for users, teams or roles.

## 6. Operator comparison (operator session)
- [ ] The same dashboard shows both customers, and the dropdown lists every customer.
- [ ] On the LBAC data sources, data without `demo="customer-isolation"` isn't returned (for example `count(up)` is empty), which confirms the operator rule is enforced too.

## Result
Isolation is validated for the demo only when **every customer** passes sections 1–5 in their own session and the operator passes section 6. Record who tested, when, and where the evidence is. Don't record a pass for a check that wasn't run.
