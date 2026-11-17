# Onboarding a customer

Adding a customer **does not** need new data sources or a new dashboard. A customer is:
- a `customer_id` label value on their telemetry;
- one team;
- one LBAC rule on each of the two existing LBAC data sources.

The shared dashboard's **Customer** dropdown is populated from query results, so the new customer appears automatically, and only for users allowed to see them.

## Steps (demo tooling)

Example: adding `customer-c` / "Customer C".

1. **Send their telemetry** to the same tenants with `customer_id="customer-c"` and `customer_name="Customer C"`. In this demo, add the customer to `CUSTOMERS` and restart the generator:
   ```bash
   # .env
   CUSTOMERS="customer-a:Customer A,customer-b:Customer B,customer-c:Customer C"
   ```
   ```bash
   ./stop.sh && ./start.sh
   ```
2. **Re-run setup.** It is idempotent: it creates `Isolation Demo - Customer C`, grants Query and folder View, adds the Explorer role, and rewrites the **complete** rule set with the new team included.
   ```bash
   python3 grafana/isolation.py plan
   python3 grafana/isolation.py setup
   ```
3. **Invite the customer's users** with no basic role. After each user's **first sign-in to the stack**, assign them:
   ```bash
   python3 grafana/isolation.py assign user-c@example.com customer-c
   ```
   `assign` refuses if the user's synced stack role isn't `None`, or if they're already in another demo team.
4. **Verify the configuration:**
   ```bash
   python3 grafana/isolation.py verify
   ```
5. **Run the [validation checklist](validation-checklist.md)** in that customer's browser session, and also in at least one existing customer's session, to confirm they can't see the new customer.

## What is reused

| Resource | Per customer? | Notes |
|---|---|---|
| Metrics and logs tenants | no | All customers share them |
| LBAC data sources | no | One Prometheus and one Loki data source for every customer |
| Dashboard and folder | no | Same dashboard; folder View granted per team |
| Team | **yes** | Decides which rule applies |
| LBAC rule | **yes** | `{ customer_id="<id>" }` on each LBAC data source |
| Team role (Explorer) | **yes** | Optional; enables Explore and Drilldown |

## Trusted customer labels

Data source LBAC trusts the labels already stored. If a customer can influence the `customer_id` on their own data, they could read or pollute another customer's view.

**This demo:** the generator sets `customer_id`, and Alloy copies it from each log line (`stage.json` → `stage.labels` in `config.alloy`). That's acceptable for synthetic data only.

**Production (proposal):**
- **Set the label where data enters, from something the customer can't change.** For example, derive it from a per-customer collector, a per-customer ingestion credential or endpoint, a Kubernetes namespace mapped through relabeling, or the authenticated tenant at a gateway.
- **Override any client-supplied `customer_id`** rather than trusting it. With Alloy, use `discovery.relabel` / `prometheus.relabel` for metrics and `loki.process` with `stage.static_labels` for logs, *per customer pipeline*.
- **Drop or quarantine data that arrives without a valid label.** It would otherwise be visible only to operators, or to nobody, depending on your rules.
- **Keep the label set small:** `customer_id` for enforcement, `customer_name` for display. Don't encode access in high-cardinality labels.

## Role verification and team membership

- **Basic role.** Customer users must have **None**. Any higher basic role normally inherits Query on the default `grafanacloud-*` data sources, which aren't LBAC-filtered.
  - With grafana.com sign-in, the stack role is synced from the Cloud organization at every sign-in, and the user only exists in the stack after their first sign-in there.
  - Check the stack role itself, not just the Cloud Portal setting.
- **One team.** Each customer user belongs to exactly one customer team, because rules from several teams combine with OR.
- **Not in Operators.** Customer users must never be in Operators.
- **Re-check periodically.** Run `verify` after any change to users, teams or roles.

## Production onboarding (proposal, not implemented)

- **Keep the customer list in one source of truth,** such as a Terraform variable or a config repository. Generate from it the teams, team roles, folder and data source permissions, and the full LBAC rule set (`grafana_data_source_config_lbac_rules` replaces all rules on each apply).
- **Manage membership in the identity provider** with team sync, so offboarding a user from the customer's group removes access.
- **Add the new customer's test identity to automated isolation tests,** and run them on every change.
