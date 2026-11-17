# Grafana Cloud customer-isolation demo

Synthetic metrics and logs demo showing customer isolation in one Grafana Cloud stack with RBAC and LBAC.

Several customers send telemetry to **one** metrics tenant and **one** logs tenant. Each customer's users see only their own data on a shared dashboard and in Explore, while an internal **Operators** team sees every customer. Isolation is enforced by [label-based access control (LBAC) for data sources](https://grafana.com/docs/grafana/latest/administration/data-source-management/teamlbac/), not by dashboard filters.

> **Status: demo.** The automation here works for setting up and checking a demo stack. It is not production-ready; see [What is demo automation vs. a production proposal](#what-is-demo-automation-vs-a-production-proposal). All data is synthetic.

## Contents

| Path | What it is |
|---|---|
| `generator.py` | Synthetic Java-style "publishing-api": Prometheus metrics endpoint + structured JSON logs (standard library only) |
| `config.alloy` | Dedicated Grafana Alloy pipeline: scrape → remote_write, tail → process → Loki |
| `start.sh`, `stop.sh`, `status.sh`, `cleanup.sh` | Local lifecycle for the generator and the demo Alloy |
| `grafana/isolation.py` | `plan`, `setup`, `assign`, `verify`, `cleanup` for the Grafana side (standard library only) |
| `grafana/dashboard.json` | Shared dashboard (data source UIDs are placeholders filled in by `setup`) |
| `.env.example` | Every setting, with no credentials |
| `docs/onboarding.md` | Adding a customer while reusing the same data sources and dashboard |
| `docs/validation-checklist.md` | Isolation tests to run in each customer's own browser session |
| `tests/` | Unit tests (generator output; `isolation.py` against a stub Grafana API) |

## Architecture

```
generator.py ──/metrics──► demo Alloy ──remote_write──► Grafana Cloud Metrics ─┐
            └──JSON log──►            ──push─────────► Grafana Cloud Logs   ─┤ data is stored once
                                                                              │
          Grafana data sources (query connections to the same tenants)       │
          ├── default grafanacloud-* ............ unrestricted, internal only ┤
          └── customer-isolation-lbac-prom/-logs  LBAC rule per team ─────────┘
                       │
                       └── shared dashboard ──► Customer A team: { customer_id="customer-a" }
                                              ├► Customer B team: { customer_id="customer-b" }
                                              └► Operators team:  { demo="customer-isolation" }
```

- **The extra data sources don't duplicate data.** The two LBAC data sources are additional *query connections* to the same tenants used by the stack's default data sources. Nothing is ingested or stored twice.
- **Enforcement is on the server.** Grafana attaches the querying user's team rule to every query sent through an LBAC data source, and Grafana Cloud filters the result. Removing filters in a panel or in Explore can't widen what a customer sees.
- **One dashboard, different results.** The shared dashboard contains no customer names or IDs. Customers and operators run the same queries and get different results.

### Synthetic telemetry

Every series and stream has `demo="customer-isolation"`, `customer_id`, `customer_name`, `service_name="publishing-api"` and `environment="demo"`. `request_id` is only in the log body, never a metric label or Loki index label.

| Metric | Type | Extra labels |
|---|---|---|
| `publishing_http_requests_total` | counter | `route` (5), `method`, `status` (200/201/304/400/404/500/503) |
| `publishing_http_request_duration_seconds` | histogram, 11 buckets | `route` |
| `jvm_memory_used_bytes`, `jvm_memory_max_bytes` | gauge | `area="heap"` |

**Logs** are JSON lines with `timestamp`, `level`, `logger`, `request_id` and a message that names the customer, so any leak is visible in the text. They cover successful requests, 4xx, 500s, database timeouts (503) and JVM heap warnings.

**Profiles.** These alternate by position in `CUSTOMERS`:

| Profile | Applies to | Behavior |
|---|---|---|
| healthy | 1st, 3rd, … (Customer A) | ~40 req/s, ~0.4% 5xx, 4 GiB heap |
| recurring incident | 2nd, 4th, … (Customer B) | ~8 req/s; during the last 6 min of every 20 min: ~15–20% 5xx, p95 ~6 s, DB timeouts, heap ~90–96% of 2 GiB |

With two customers there are about 170 active series, and logs run at about 1–4 lines/s.

## Prerequisites

- A Grafana Cloud stack. Data source LBAC is GA for Loki and Prometheus in Grafana Cloud.
- Grafana Alloy and Python 3.9+ on the machine running the generator.
- Org admin access to the stack, and the ability to create Cloud Access Policies.
- Test users for each customer, who can sign in to the stack with **no basic role** (see [Role verification](#role-verification)).

### Credentials

All credentials go in `.env`, which is git-ignored. Create it with `cp .env.example .env && chmod 600 .env`. Never commit it, and never print it.

| Variable | What | Scopes and permissions |
|---|---|---|
| `GC_WRITE_TOKEN` | Cloud Access Policy token for ingestion | `metrics:write`, `logs:write` |
| `GC_READ_TOKEN` | Cloud Access Policy token stored as the LBAC data sources' password | `metrics:read`, `logs:read`, **no label selectors**; access-policy label selectors override data source LBAC |
| `GRAFANA_TOKEN` | Grafana **service account** token for `grafana/isolation.py` | Simplest: basic role **Admin**, short expiry, deleted after setup. See the list below for a least-privilege alternative. |

The push and query URLs and the instance IDs (`GC_*_URL`, `GC_*_USER`) are on the stack's details page in the Cloud Portal. Push URLs end in `/api/prom/push` and `/loki/api/v1/push`; the query URLs are the same hosts without those suffixes.

**Least-privilege alternative for `GRAFANA_TOKEN`.** Create a service account with no basic role and add these fixed roles:
- **`setup`:** Data sources: Writer, Data sources: Permission writer, Teams: Writer (or Creator), Folders: Writer, Dashboards: Writer. Plus `teams.roles:add` for the Explorer role; if it's missing, `setup` prints a warning and you assign Explorer in the UI.
- **`assign` and `verify`:** Teams: Reader, Teams: Permission reader, Organization users: Reader, Roles: Reader.

Permission sets differ between Grafana versions, so check the result with `verify`.

## Setup

```bash
cp .env.example .env && chmod 600 .env     # then fill in values
./start.sh                                 # generator + dedicated Alloy (separate port and storage path)
./status.sh

python3 grafana/isolation.py plan          # what will be created (no API calls)
python3 grafana/isolation.py setup --dry-run
python3 grafana/isolation.py setup
```

`setup` is idempotent; run it again after changing `CUSTOMERS`. It does the following:
1. **Teams:** creates one team per customer plus Operators. If Grafana adds the creating user to a new team, `setup` removes them so the teams start empty.
2. **Folder:** creates the folder, with View for customer teams and Edit for Operators. The Viewer and Editor role grants are removed.
3. **LBAC data sources:** creates or updates the Prometheus and Loki data sources with basic auth and `GC_READ_TOKEN`.
4. **Data source permissions:** removes the Viewer and Editor role grants from both, and gives every demo team Query.
5. **LBAC rules:** writes one rule per team, `{ customer_id="<id>" }` for customers and `OPERATOR_RULE` for Operators. The API replaces the whole rule set, so every team is always sent.
6. **Explore:** assigns **Data sources: Explorer** to each customer team.
7. **Dashboard:** uploads it into the folder.

Then assign users and check the configuration:

```bash
python3 grafana/isolation.py assign user-a@example.com customer-a
python3 grafana/isolation.py assign user-b@example.com customer-b
python3 grafana/isolation.py assign operator@example.com operators
python3 grafana/isolation.py verify
```

Finally, have each customer user run [docs/validation-checklist.md](docs/validation-checklist.md) in their own browser session.

## RBAC and LBAC model

| Principal | Basic role | Team | Access |
|---|---|---|---|
| Customer user | **None** | exactly one customer team | Query on the two LBAC data sources only (team grant), View on the demo folder, Explore (team role), filtered by `{ customer_id="<id>" }` |
| Operator | Admin (or as needed) | Operators | All customers on the LBAC data sources through `{ demo="customer-isolation" }`; unrestricted default data sources through their basic role |

Behaviors this design relies on, from the Grafana docs:
- **No rule means no restriction.** A user whose teams have no LBAC rule can query **all** data on an LBAC data source. Only teams with rules are given Query, and `verify` fails if a team can query without a rule.
- **Rules from several teams combine with OR**, so each customer user must be in **exactly one** customer team. `assign` refuses a second customer team.
- **Admins are filtered too.** An admin who belongs to a team with rules is filtered by those rules. Operator access is granted by an explicit rule, not assumed from the admin role.
- **Data sources must be created manually.** LBAC rules can't be added to the default provisioned `grafanacloud-*` data sources.
- **Access policies override LBAC.** A Cloud Access Policy's label selectors override data source LBAC, so the read token must not have any.

### Role verification

The default `grafanacloud-*` data sources normally give **Query to the Viewer and Editor basic roles**. A customer user with any basic role above None can therefore query them and bypass LBAC entirely. The tooling guards against this in three places:
- `assign` refuses customer users whose stack role isn't `None`.
- `verify` fails if any customer-team member's role drifts.
- `verify` lists the other data sources that point at the same backends.

When users sign in through grafana.com, their **stack** role is synced from the Cloud organization on each sign-in, and it can't be changed inside the stack. The stack user only appears after their first sign-in to the stack itself; accepting the invite isn't enough. Always confirm the *synced stack role* before assigning a user, because a "no basic role" setting in the Cloud Portal may not appear in the stack as `None`.

### Team membership

Team membership is what decides which rule applies, so treat team changes as security changes:
- Put each customer user in one customer team only.
- Keep customer users out of Operators.
- Keep Operators small.

Removing a user from their team removes their access to the LBAC data sources. A user in no demo team can't query those data sources at all.

### Trusted customer labels

**Isolation is only as strong as the `customer_id` label.** In this demo, the generator writes the label and Alloy copies it from the log line. That's fine for synthetic data, but it means anything that controls the payload controls the label. In production, the label should come from where the data enters, not from the application. See [docs/onboarding.md](docs/onboarding.md#trusted-customer-labels).

## Lifecycle and cleanup

| Command | Effect |
|---|---|
| `./start.sh` / `./stop.sh` / `./status.sh` | Run, stop and inspect the generator and demo Alloy (pid files in `data/`) |
| `./cleanup.sh` | Stop them and delete local state (`data/`) |
| `python3 grafana/isolation.py cleanup` | Delete the demo dashboard, folder, LBAC data sources and teams |

The generator stops on its own after `DEMO_MAX_HOURS` (default 168). It doesn't run while the machine sleeps.

Stored telemetry isn't deleted; it ages out with normal retention. After cleanup, revoke the access policy tokens and delete the setup service account.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

The tests check the generator's labels, cardinality, histogram structure and profiles. They also run `isolation.py` against an **in-memory stub** of the Grafana HTTP API: setup requests, rule payloads, permission changes, `assign` guards, `verify` failure detection, and that tokens are never printed.

The stub tests prove the tool sends the intended requests. They **don't** prove enforcement in Grafana Cloud; only the browser checklist does that.

## What is demo automation vs. a production proposal

**Works in this repo (demo automation):**
- the synthetic generator and Alloy pipeline;
- idempotent `setup` of the teams, LBAC data sources, permissions, rules, folder, Explore role and dashboard;
- guarded `assign` (None role, one team);
- configuration `verify`;
- `cleanup`;
- unit tests.

**Proposed for production (not implemented here):**
- **Set `customer_id` from a trusted source** at ingestion, and reject or quarantine data without a valid label.
- **Manage teams, permissions, rules and role assignments as code.** For example, Terraform's `grafana_team`, `grafana_data_source_permission`, `grafana_data_source_config_lbac_rules` (which manages the *entire* rule set) and `grafana_role_assignment`. The LBAC data sources themselves still have to be created manually.
- **Drive team membership from the identity provider** (SAML/SCIM team sync) instead of manual assignment.
- **Run `verify`-style drift checks continuously**, and alert on role drift, multi-team membership, or Viewer/Editor grants reappearing on the LBAC data sources.
- **Automate the isolation tests** with per-customer test identities.
- **Review other surfaces** customers might reach: alert rules, Drilldown apps (enabled by the Explorer role), shared or public dashboards, snapshots and app plugins.
- **Consider removing Viewer/Editor grants from the default data sources** as defense in depth. This affects every Viewer/Editor principal in the stack.

## Limitations

- Traces LBAC is in public preview and isn't covered here.
- Cross-tenant queries aren't supported with LBAC, and a data source supports roughly 5,000–6,000 rules.
- LBAC filters by labels; it doesn't redact log line contents. Don't put other customers' identifiers or secrets in log bodies.
- Loki label discovery is answered at index (chunk) granularity, so recently retired label values can briefly appear within the streams a user is allowed to see.
- Series without `customer_id` (for example `up`) are invisible to customers by design.
- If `GC_READ_TOKEN` expires, the LBAC data sources stop working. Track its expiry.
