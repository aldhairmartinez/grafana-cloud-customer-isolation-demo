#!/usr/bin/env python3
"""Configure, assign, verify and clean up the customer-isolation demo in a Grafana Cloud stack.

    python3 grafana/isolation.py plan                     # show what will be configured (no API calls)
    python3 grafana/isolation.py setup [--dry-run]        # teams, LBAC data sources, permissions, rules, folder, dashboard
    python3 grafana/isolation.py assign <email> <customer-id|operators>
    python3 grafana/isolation.py verify                   # read back config and flag isolation risks
    python3 grafana/isolation.py cleanup [--yes]          # delete everything `setup` created

Configuration comes from environment variables, falling back to ./.env (see .env.example).
Tokens are sent only in request headers and are never printed.
Standard library only.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPLORER_ROLE = "fixed:datasources:explorer"
PERM_VIEW, PERM_EDIT = 1, 2

DEFAULTS = {
    "CUSTOMERS": "customer-a:Customer A,customer-b:Customer B",
    "TEAM_PREFIX": "Isolation Demo",
    "OPERATOR_RULE": '{ demo="customer-isolation" }',
    "FOLDER_UID": "customer-isolation-demo",
    "FOLDER_TITLE": "Customer Isolation Demo",
    "DASHBOARD_UID": "customer-isolation",
    "DS_PROM_UID": "customer-isolation-lbac-prom",
    "DS_LOGS_UID": "customer-isolation-lbac-logs",
}


class ConfigError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, method, path, status, body):
        super().__init__(f"{method} {path} -> HTTP {status}: {body}")
        self.status = status


# ---------------------------------------------------------------------------- config

def load_env_file(path):
    """Minimal KEY=VALUE parser (supports single/double quotes and comments)."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if val[:1] in ("'", '"'):
                end = val.find(val[0], 1)  # closing quote; anything after it (e.g. a comment) is ignored
                val = val[1:end] if end != -1 else val[1:]
            elif " #" in val:
                val = val.split(" #", 1)[0].rstrip()
            values[key] = val
    return values


def parse_customers(spec):
    customers = []
    for item in (x for x in spec.split(",") if x.strip()):
        if ":" not in item:
            raise ConfigError(f"bad CUSTOMERS entry {item!r}; expected id:Display Name")
        cid, name = (s.strip() for s in item.split(":", 1))
        if not cid or not name or '"' in cid + name or cid == "operators":
            raise ConfigError(f"bad CUSTOMERS entry {item!r}")
        customers.append({"id": cid, "name": name})
    if not customers:
        raise ConfigError("CUSTOMERS is empty")
    if len({c["id"] for c in customers}) != len(customers):
        raise ConfigError("duplicate customer ids in CUSTOMERS")
    return customers


class Config:
    def __init__(self, env):
        get = lambda k: env.get(k) or DEFAULTS.get(k, "")
        self.env = env
        self.customers = parse_customers(get("CUSTOMERS"))
        self.team_prefix = get("TEAM_PREFIX")
        self.operator_rule = get("OPERATOR_RULE")
        self.folder_uid, self.folder_title = get("FOLDER_UID"), get("FOLDER_TITLE")
        self.dashboard_uid = get("DASHBOARD_UID")
        self.ds_prom_uid, self.ds_logs_uid = get("DS_PROM_UID"), get("DS_LOGS_UID")

    def team_name(self, key):
        if key == "operators":
            return f"{self.team_prefix} - Operators"
        return f"{self.team_prefix} - {self.customer(key)['name']}"

    def customer(self, cid):
        for c in self.customers:
            if c["id"] == cid:
                return c
        raise ConfigError(f"unknown customer id {cid!r}; configured: {[c['id'] for c in self.customers]}")

    def rule(self, key):
        return self.operator_rule if key == "operators" else '{ customer_id="%s" }' % key

    def team_keys(self):
        return [c["id"] for c in self.customers] + ["operators"]

    def require(self, *names):
        missing = [n for n in names if not self.env.get(n) or "<" in self.env.get(n, "")]
        if missing:
            raise ConfigError("missing or placeholder values in environment/.env: " + ", ".join(missing))
        return [self.env[n] for n in names]


# ---------------------------------------------------------------------------- HTTP

class Api:
    def __init__(self, base_url, token, dry_run=False, out=print):
        self.base = base_url.rstrip("/")
        self.token = token
        self.dry_run = dry_run
        self.out = out

    def req(self, method, path, body=None, allow=()):
        """Return (status, json). Raises ApiError for non-2xx statuses not in `allow`."""
        if self.dry_run and method != "GET":
            self.out(f"  [dry-run] {method} {path} {redact(body) if body is not None else ''}".rstrip())
            return 200, {}
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Authorization", "Bearer " + self.token)
        r.add_header("Accept", "application/json")
        if data is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw": raw.decode(errors="replace")[:300]}
        if 200 <= status < 300 or status in allow:
            return status, parsed
        raise ApiError(method, path, status, redact(parsed))


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in ("secureJsonData", "basicAuthPassword", "password", "token", "key")
                    else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------- lookups

def find_team(api, name):
    _, data = api.req("GET", "/api/teams/search?perpage=100&name=" + urllib.parse.quote(name))
    for t in data.get("teams") or []:
        if t.get("name") == name:
            return t
    return None


def team_members(api, team_id):
    _, data = api.req("GET", f"/api/teams/{team_id}/members")
    return data if isinstance(data, list) else []


def find_org_user(api, email):
    _, data = api.req("GET", "/api/org/users/search?perpage=20&query=" + urllib.parse.quote(email))
    for u in data.get("orgUsers") or []:
        if (u.get("email") or "").lower() == email.lower():
            return u
    return None


def get_ds(api, uid):
    status, data = api.req("GET", f"/api/datasources/uid/{uid}", allow=(404,))
    return None if status == 404 else data


def lbac_rules(api, uid):
    _, data = api.req("GET", f"/api/datasources/uid/{uid}/lbac/teams")
    rules = {}
    for entry in data.get("rules") or []:
        team_uid = entry.get("teamUid") or entry.get("teamUId")
        rules[team_uid] = entry.get("rules") or []
    return rules


def role_uid(api, name):
    _, data = api.req("GET", "/api/access-control/roles")
    for r in data if isinstance(data, list) else []:
        if r.get("name") == name:
            return r.get("uid")
    return None


def team_role_names(api, team_id):
    _, data = api.req("GET", f"/api/access-control/teams/{team_id}/roles")
    return {r.get("name") for r in data} if isinstance(data, list) else set()


# ---------------------------------------------------------------------------- setup

def ensure_team(api, name, out):
    team = find_team(api, name)
    if team:
        out(f"team exists: {name} (id {team['id']})")
        return team
    _, created = api.req("POST", "/api/teams", {"name": name})
    if api.dry_run:
        return {"id": 0, "uid": f"<new:{name}>", "name": name}
    team_id = created["teamId"]
    _, team = api.req("GET", f"/api/teams/{team_id}")
    # Grafana may add the creating user as a team admin; demo teams must start empty
    # because team membership decides which LBAC rules apply.
    for m in team_members(api, team_id):
        api.req("DELETE", f"/api/teams/{team_id}/members/{m['userId']}")
        out(f"  removed auto-added member {m.get('login')} from new team {name}")
    out(f"team created: {name} (id {team_id})")
    return team


def datasource_body(kind, uid, url, user, password, existing=None):
    body = dict(existing or {})
    for k in ("id", "orgId", "version", "readOnly", "typeLogoUrl", "secureJsonFields"):
        body.pop(k, None)
    json_data = dict(body.get("jsonData") or {})  # keep teamHttpHeaders (where Grafana stores LBAC rules)
    if kind == "prometheus":
        json_data.update({"prometheusType": "Mimir", "httpMethod": "POST", "timeInterval": "15s"})
    else:
        json_data.update({"timeout": "300"})
    body.update({"uid": uid, "name": uid, "type": kind, "access": "proxy", "url": url,
                 "basicAuth": True, "basicAuthUser": user, "jsonData": json_data,
                 "secureJsonData": {"basicAuthPassword": password}})
    return body


def setup(cfg, api, out=print):
    metrics_url, metrics_user, logs_url, logs_user, read_token = cfg.require(
        "GC_METRICS_QUERY_URL", "GC_METRICS_USER", "GC_LOGS_QUERY_URL", "GC_LOGS_USER", "GC_READ_TOKEN")
    warnings = []

    out("== teams")
    teams = {k: ensure_team(api, cfg.team_name(k), out) for k in cfg.team_keys()}

    out("== folder")
    status, _ = api.req("GET", f"/api/folders/{cfg.folder_uid}", allow=(404,))
    if status == 404:
        api.req("POST", "/api/folders", {"uid": cfg.folder_uid, "title": cfg.folder_title})
        out(f"folder created: {cfg.folder_title}")
    else:
        out(f"folder exists: {cfg.folder_uid}")
    items = [{"teamId": teams[c["id"]]["id"], "permission": PERM_VIEW} for c in cfg.customers]
    items.append({"teamId": teams["operators"]["id"], "permission": PERM_EDIT})
    # Replaces the folder's permission list: removes the default Viewer/Editor role grants.
    # Org admins keep access through their basic role.
    api.req("POST", f"/api/folders/{cfg.folder_uid}/permissions", {"items": items})
    out("folder permissions: customer teams View, Operators Edit, no Viewer/Editor role grants")

    out("== LBAC data sources")
    for kind, uid, url, user in (("prometheus", cfg.ds_prom_uid, metrics_url, metrics_user),
                                 ("loki", cfg.ds_logs_uid, logs_url, logs_user)):
        existing = get_ds(api, uid)
        body = datasource_body(kind, uid, url, user, read_token, existing)
        if existing:
            api.req("PUT", f"/api/datasources/uid/{uid}", body)
            out(f"data source updated: {uid}")
        else:
            api.req("POST", "/api/datasources", body)
            out(f"data source created: {uid}")

        api.req("POST", f"/api/access-control/datasources/{uid}/builtInRoles/Viewer", {"permission": ""})
        api.req("POST", f"/api/access-control/datasources/{uid}/builtInRoles/Editor", {"permission": ""})
        for k in cfg.team_keys():
            api.req("POST", f"/api/access-control/datasources/{uid}/teams/{teams[k]['id']}", {"permission": "Query"})
        out(f"  permissions: Viewer/Editor role grants removed; Query for {len(teams)} demo teams")

        # The rules API replaces the complete rule set, so always send every team.
        rules = [{"teamUid": teams[k]["uid"], "rules": [cfg.rule(k)]} for k in cfg.team_keys()]
        api.req("PUT", f"/api/datasources/uid/{uid}/lbac/teams", {"rules": rules})
        out(f"  LBAC rules written for {len(rules)} teams")

    out("== Explore access for customer teams")
    explorer = None if api.dry_run else role_uid(api, EXPLORER_ROLE)
    for c in cfg.customers:
        t = teams[c["id"]]
        try:
            if api.dry_run:
                api.req("POST", f"/api/access-control/teams/{t['id']}/roles", {"roleUid": f"<{EXPLORER_ROLE}>"})
            elif EXPLORER_ROLE in team_role_names(api, t["id"]):
                out(f"  {t['name']}: Explorer already assigned")
            elif not explorer:
                raise ApiError("GET", "/api/access-control/roles", 0, f"{EXPLORER_ROLE} not found")
            else:
                api.req("POST", f"/api/access-control/teams/{t['id']}/roles", {"roleUid": explorer})
                out(f"  {t['name']}: Explorer assigned")
        except ApiError as e:
            warnings.append(f"Could not assign 'Data sources: Explorer' to team '{t['name']}' ({e.status}). "
                            "Assign it in the UI: Administration > Teams > team > role picker.")

    out("== dashboard")
    with open(os.path.join(ROOT, "grafana", "dashboard.json")) as f:
        raw = f.read()
    raw = raw.replace("__DS_PROM_UID__", cfg.ds_prom_uid).replace("__DS_LOGS_UID__", cfg.ds_logs_uid)
    dash = json.loads(raw)
    dash["uid"] = cfg.dashboard_uid
    dash.pop("id", None)
    api.req("POST", "/api/dashboards/db", {"dashboard": dash, "folderUid": cfg.folder_uid,
                                           "overwrite": True, "message": "customer-isolation demo setup"})
    out(f"dashboard uploaded: {api.base}/d/{cfg.dashboard_uid}")

    for w in warnings:
        out("WARNING: " + w)
    out("setup finished. Next: assign users with `assign`, then run `verify`.")
    return warnings


# ---------------------------------------------------------------------------- assign

def assign(cfg, api, email, key, out=print):
    if key != "operators":
        cfg.customer(key)  # validates the id
    user = find_org_user(api, email)
    if not user:
        out(f"STOP: no stack user with email {email}. They must accept the invite and sign in to the stack once.")
        return 1
    role = user.get("role")
    out(f"user {user.get('login')} (id {user['userId']}) stack role: {role}")
    if key != "operators" and role != "None":
        out(f"STOP: customer users must have basic role None, not {role!r}. A Viewer/Editor/Admin role "
            "inherits Query on the default unrestricted data sources and bypasses LBAC.")
        return 2

    target = cfg.team_name(key)
    membership = {}
    for k in cfg.team_keys():
        t = find_team(api, cfg.team_name(k))
        if not t:
            out(f"STOP: team {cfg.team_name(k)!r} not found - run setup first.")
            return 1
        membership[k] = (t, any(m.get("userId") == user["userId"] for m in team_members(api, t["id"])))

    others = [cfg.team_name(k) for k, (_, member) in membership.items() if member and k != key]
    if others:
        out(f"STOP: user is already in {others}. Rules from several teams are combined (OR), "
            "so a user must belong to exactly one demo team. Remove them there first.")
        return 3
    team, already = membership[key]
    if already:
        out(f"already a member of {target}; nothing to do")
        return 0
    api.req("POST", f"/api/teams/{team['id']}/members", {"userId": user["userId"]})
    out(f"added to {target}. Run `verify`, then the validation checklist in the README.")
    return 0


# ---------------------------------------------------------------------------- verify

def verify(cfg, api, out=print):
    fails, warns = [], []
    teams = {k: find_team(api, cfg.team_name(k)) for k in cfg.team_keys()}
    for k, t in teams.items():
        if not t:
            fails.append(f"team {cfg.team_name(k)!r} missing")
    if fails:
        for f in fails:
            out("FAIL " + f)
        return 1
    by_uid = {t["uid"]: k for k, t in teams.items()}
    by_id = {t["id"]: k for k, t in teams.items()}

    out("== data sources")
    for uid in (cfg.ds_prom_uid, cfg.ds_logs_uid):
        if not get_ds(api, uid):
            fails.append(f"data source {uid} missing")
            continue
        _, perms = api.req("GET", f"/api/access-control/datasources/{uid}")
        rules = lbac_rules(api, uid)
        for p in perms:
            role, team_id, user_login = p.get("builtInRole"), p.get("teamId"), p.get("userLogin")
            if role in ("Viewer", "Editor", "None"):
                fails.append(f"{uid}: basic role {role} has {p.get('permission')} - remove it")
            elif team_id and team_id not in by_id:
                warns.append(f"{uid}: non-demo team {p.get('team')} has {p.get('permission')}")
            elif user_login and p.get("permission") != "Admin":
                warns.append(f"{uid}: user {user_login} has a direct {p.get('permission')} grant")
        query_team_uids = {p.get("teamUid") for p in perms if p.get("teamId")}
        for team_uid in query_team_uids:
            if team_uid and not rules.get(team_uid):
                fails.append(f"{uid}: team {team_uid} can query but has NO LBAC rule (it would see all data)")
        for k in cfg.team_keys():
            got = rules.get(teams[k]["uid"])
            if got != [cfg.rule(k)]:
                fails.append(f"{uid}: rule for {cfg.team_name(k)} is {got}, expected [{cfg.rule(k)}]")
        out(f"  {uid}: {len(perms)} permission entries, {len(rules)} rule sets checked")

    out("== team members")
    seen = {}
    for k, t in teams.items():
        members = team_members(api, t["id"])
        out(f"  {t['name']}: {', '.join(m.get('login', '?') for m in members) or '(empty)'}")
        for m in members:
            seen.setdefault(m["userId"], []).append(k)
        if k != "operators":
            if EXPLORER_ROLE not in team_role_names(api, t["id"]):
                warns.append(f"{t['name']} lacks 'Data sources: Explorer' (no Explore for its members)")
            for m in members:
                u = find_org_user(api, m.get("email") or "")
                role = u.get("role") if u else "?"
                if role != "None":
                    fails.append(f"customer user {m.get('login')} in {t['name']} has basic role {role!r}, must be None")
    for user_id, keys in seen.items():
        if len(keys) > 1:
            fails.append(f"user id {user_id} is in several demo teams {keys}; rules would be combined")

    out("== folder")
    _, fperms = api.req("GET", f"/api/folders/{cfg.folder_uid}/permissions", allow=(404,))
    for p in fperms if isinstance(fperms, list) else []:
        if p.get("role") in ("Viewer", "Editor"):
            warns.append(f"folder grants {p['permission']} to basic role {p['role']}")

    out("== other data sources on the same backends (customers must not reach these)")
    _, all_ds = api.req("GET", "/api/datasources")
    targets = {(get_ds(api, u) or {}).get("url") for u in (cfg.ds_prom_uid, cfg.ds_logs_uid)}
    for ds in all_ds if isinstance(all_ds, list) else []:
        if ds.get("uid") in (cfg.ds_prom_uid, cfg.ds_logs_uid) or ds.get("url") not in targets:
            continue
        status, perms = api.req("GET", f"/api/access-control/datasources/{ds['uid']}", allow=(403,))
        roles = sorted({p.get("builtInRole") for p in perms if p.get("builtInRole") and p.get("builtInRole") != "Admin"}) \
            if status != 403 else ["<cannot read permissions>"]
        out(f"  {ds['name']}: role grants {roles or 'none'} - safe only while customer users have basic role None")

    for w in warns:
        out("WARN " + w)
    for f in fails:
        out("FAIL " + f)
    out("configuration checks passed" if not fails else f"{len(fails)} configuration check(s) failed")
    out("Note: this verifies configuration only. Isolation is validated by the customer-session browser tests.")
    return 1 if fails else 0


# ---------------------------------------------------------------------------- cleanup

def cleanup(cfg, api, out=print):
    api.req("DELETE", f"/api/dashboards/uid/{cfg.dashboard_uid}", allow=(404,))
    api.req("DELETE", f"/api/folders/{cfg.folder_uid}", allow=(404,))
    for uid in (cfg.ds_prom_uid, cfg.ds_logs_uid):
        api.req("DELETE", f"/api/datasources/uid/{uid}", allow=(404,))
    for k in cfg.team_keys():
        t = find_team(api, cfg.team_name(k))
        if t:
            api.req("DELETE", f"/api/teams/{t['id']}")
    out("demo dashboard, folder, LBAC data sources and teams removed (stored telemetry is untouched)")
    return 0


# ---------------------------------------------------------------------------- CLI

def plan(cfg, out=print):
    out(f"customers: {', '.join(c['id'] + ' (' + c['name'] + ')' for c in cfg.customers)}")
    out("teams and LBAC rules (applied to both data sources):")
    for k in cfg.team_keys():
        out(f"  {cfg.team_name(k):40} {cfg.rule(k)}")
    out(f"data sources: {cfg.ds_prom_uid}, {cfg.ds_logs_uid} (Query for demo teams only)")
    out(f"folder: {cfg.folder_title} ({cfg.folder_uid}); dashboard uid: {cfg.dashboard_uid}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=os.path.join(ROOT, ".env"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    s = sub.add_parser("setup")
    s.add_argument("--dry-run", action="store_true", help="read only; print the write requests instead of sending them")
    a = sub.add_parser("assign")
    a.add_argument("email")
    a.add_argument("team", help="customer id from CUSTOMERS, or 'operators'")
    sub.add_parser("verify")
    c = sub.add_parser("cleanup")
    c.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    env = {**load_env_file(args.env_file), **os.environ}
    try:
        cfg = Config(env)
        if args.cmd == "plan":
            return plan(cfg)
        url, token = cfg.require("GRAFANA_URL", "GRAFANA_TOKEN")
        api = Api(url, token, dry_run=getattr(args, "dry_run", False))
        if args.cmd == "setup":
            setup(cfg, api)
            return 0
        if args.cmd == "assign":
            return assign(cfg, api, args.email, args.team)
        if args.cmd == "verify":
            return verify(cfg, api)
        if args.cmd == "cleanup":
            if not args.yes and input("Delete the demo dashboard, folder, data sources and teams? [y/N] ").lower() != "y":
                return 1
            return cleanup(cfg, api)
    except (ConfigError, ApiError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
