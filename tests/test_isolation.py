"""Tests grafana/isolation.py against an in-memory stub of the Grafana HTTP API.

The stub only models the endpoints the tool uses. It checks the *requests the tool
makes*; it does not prove anything about real Grafana Cloud enforcement.
"""
import json
import os
import re
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "grafana"))
import isolation as iso  # noqa: E402

TOKEN = "test-token-not-a-secret"


class FakeGrafana:
    def __init__(self):
        self.users = {4: {"userId": 4, "login": "admin", "email": "admin@example.com", "role": "Admin"},
                      40: {"userId": 40, "login": "user-a", "email": "user-a@example.com", "role": "None"},
                      41: {"userId": 41, "login": "user-b", "email": "user-b@example.com", "role": "None"},
                      42: {"userId": 42, "login": "viewer", "email": "viewer@example.com", "role": "Viewer"}}
        self.teams, self.members, self.team_roles = {}, {}, {}
        self.datasources = {"grafanacloud-prom": {"uid": "grafanacloud-prom", "name": "grafanacloud-prom",
                                                  "url": "https://metrics.example.com/api/prom"}}
        self.ds_perms = {"grafanacloud-prom": [{"builtInRole": "Admin", "permission": "Admin"},
                                               {"builtInRole": "Viewer", "permission": "Query"}]}
        self.lbac, self.folders, self.folder_perms, self.dashboards = {}, {}, {}, {}
        self.next_id = 100
        self.creator = 4  # Grafana adds the creating user to new teams
        self.requests = []

    def _id(self):
        self.next_id += 1
        return self.next_id

    def handle(self, method, path, body):
        self.requests.append((method, path, body))
        u = urllib.parse.urlparse(path)
        p, q = u.path, urllib.parse.parse_qs(u.query)
        m = lambda pattern: re.fullmatch(pattern, p)

        if m(r"/api/teams/search"):
            name = q.get("name", [""])[0]
            return 200, {"teams": [t for t in self.teams.values() if t["name"] == name]}
        if method == "POST" and p == "/api/teams":
            tid = self._id()
            self.teams[tid] = {"id": tid, "uid": f"team-uid-{tid}", "name": body["name"]}
            self.members[tid] = [self.creator]
            self.team_roles[tid] = []
            return 200, {"teamId": tid, "uid": f"team-uid-{tid}"}
        if (r := m(r"/api/teams/(\d+)")):
            tid = int(r.group(1))
            if method == "DELETE":
                self.teams.pop(tid)
                return 200, {}
            return 200, self.teams[tid]
        if (r := m(r"/api/teams/(\d+)/members")):
            tid = int(r.group(1))
            if method == "POST":
                self.members[tid].append(body["userId"])
                return 200, {}
            return 200, [{**self.users[i], "teamId": tid} for i in self.members[tid]]
        if (r := m(r"/api/teams/(\d+)/members/(\d+)")) and method == "DELETE":
            self.members[int(r.group(1))].remove(int(r.group(2)))
            return 200, {}
        if p == "/api/org/users/search":
            needle = q.get("query", [""])[0]
            return 200, {"orgUsers": [x for x in self.users.values() if needle in x["email"]]}

        if (r := m(r"/api/folders/([^/]+)")) and method == "GET":
            return (200, self.folders[r.group(1)]) if r.group(1) in self.folders else (404, {})
        if (r := m(r"/api/folders/([^/]+)")) and method == "DELETE":
            self.folders.pop(r.group(1), None)
            return 200, {}
        if method == "POST" and p == "/api/folders":
            self.folders[body["uid"]] = body
            self.folder_perms[body["uid"]] = [{"role": "Viewer", "permission": 1}, {"role": "Editor", "permission": 2}]
            return 200, body
        if (r := m(r"/api/folders/([^/]+)/permissions")):
            if method == "POST":
                self.folder_perms[r.group(1)] = body["items"]
                return 200, {}
            return 200, self.folder_perms.get(r.group(1), [])

        if method == "GET" and p == "/api/datasources":
            return 200, list(self.datasources.values())
        if method == "POST" and p == "/api/datasources":
            self.datasources[body["uid"]] = body
            self.ds_perms[body["uid"]] = [{"builtInRole": "Admin", "permission": "Admin"},
                                          {"builtInRole": "Viewer", "permission": "Query"},
                                          {"builtInRole": "Editor", "permission": "Query"}]
            return 200, {"datasource": body}
        if (r := m(r"/api/datasources/uid/([^/]+)")):
            uid = r.group(1)
            if method == "GET":
                if uid not in self.datasources:
                    return 404, {}
                ds = dict(self.datasources[uid])
                ds.pop("secureJsonData", None)
                return 200, ds
            if method == "PUT":
                self.datasources[uid] = body
                return 200, {}
            if method == "DELETE":
                self.datasources.pop(uid, None)
                return 200, {}
        if (r := m(r"/api/datasources/uid/([^/]+)/lbac/teams")):
            if method == "PUT":
                self.lbac[r.group(1)] = body["rules"]
                return 200, {}
            return 200, {"rules": self.lbac.get(r.group(1), [])}
        if (r := m(r"/api/access-control/datasources/([^/]+)")):
            return 200, self.ds_perms.get(r.group(1), [])
        if (r := m(r"/api/access-control/datasources/([^/]+)/builtInRoles/(\w+)")):
            perms = [x for x in self.ds_perms[r.group(1)] if x.get("builtInRole") != r.group(2)]
            if body["permission"]:
                perms.append({"builtInRole": r.group(2), "permission": body["permission"]})
            self.ds_perms[r.group(1)] = perms
            return 200, {}
        if (r := m(r"/api/access-control/datasources/([^/]+)/teams/(\d+)")):
            tid = int(r.group(2))
            perms = [x for x in self.ds_perms[r.group(1)] if x.get("teamId") != tid]
            perms.append({"teamId": tid, "teamUid": self.teams[tid]["uid"], "team": self.teams[tid]["name"],
                          "permission": body["permission"]})
            self.ds_perms[r.group(1)] = perms
            return 200, {}

        if p == "/api/access-control/roles":
            return 200, [{"uid": "explorer-uid", "name": iso.EXPLORER_ROLE}]
        if (r := m(r"/api/access-control/teams/(\d+)/roles")):
            tid = int(r.group(1))
            if method == "POST":
                self.team_roles[tid].append(iso.EXPLORER_ROLE)
                return 200, {}
            return 200, [{"name": n} for n in self.team_roles[tid]]

        if method == "POST" and p == "/api/dashboards/db":
            self.dashboards[body["dashboard"]["uid"]] = body
            return 200, {"status": "success"}
        if (r := m(r"/api/dashboards/uid/([^/]+)")) and method == "DELETE":
            self.dashboards.pop(r.group(1), None)
            return 200, {}
        return 404, {"message": f"stub: no route for {method} {p}"}


class StubServer:
    def __init__(self, fake):
        class H(BaseHTTPRequestHandler):
            def _do(self):
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    self.send_response(401)
                    self.end_headers()
                    return
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                status, data = fake.handle(self.command, self.path, body)
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            do_GET = do_POST = do_PUT = do_DELETE = _do

            def log_message(self, *a):
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


ENV = {"GC_METRICS_QUERY_URL": "https://metrics.example.com/api/prom", "GC_METRICS_USER": "1111",
       "GC_LOGS_QUERY_URL": "https://logs.example.com", "GC_LOGS_USER": "2222",
       "GC_READ_TOKEN": "read-token-not-a-secret"}


class IsolationToolTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGrafana()
        self.server = StubServer(self.fake)
        self.cfg = iso.Config(dict(ENV))
        self.out = []
        self.api = iso.Api(self.server.url, TOKEN, out=self.out.append)
        iso.setup(self.cfg, self.api, out=self.out.append)
        self.teams = {t["name"]: t for t in self.fake.teams.values()}

    def tearDown(self):
        self.server.httpd.shutdown()
        self.server.httpd.server_close()

    def team(self, key):
        return self.teams[self.cfg.team_name(key)]

    def test_setup_creates_empty_teams(self):
        self.assertEqual(set(self.teams), {"Isolation Demo - Customer A", "Isolation Demo - Customer B",
                                           "Isolation Demo - Operators"})
        for t in self.teams.values():
            self.assertEqual(self.fake.members[t["id"]], [], "auto-added creator must be removed")

    def test_lbac_datasource_permissions(self):
        for uid in (self.cfg.ds_prom_uid, self.cfg.ds_logs_uid):
            perms = self.fake.ds_perms[uid]
            self.assertFalse([p for p in perms if p.get("builtInRole") in ("Viewer", "Editor")])
            self.assertEqual({p["teamId"] for p in perms if p.get("teamId")}, {t["id"] for t in self.teams.values()})
            ds = self.fake.datasources[uid]
            self.assertTrue(ds["basicAuth"])
            self.assertEqual(ds["secureJsonData"]["basicAuthPassword"], ENV["GC_READ_TOKEN"])

    def test_lbac_rules_cover_every_team(self):
        for uid in (self.cfg.ds_prom_uid, self.cfg.ds_logs_uid):
            rules = {r["teamUid"]: r["rules"] for r in self.fake.lbac[uid]}
            self.assertEqual(rules[self.team("customer-a")["uid"]], ['{ customer_id="customer-a" }'])
            self.assertEqual(rules[self.team("customer-b")["uid"]], ['{ customer_id="customer-b" }'])
            self.assertEqual(rules[self.team("operators")["uid"]], ['{ demo="customer-isolation" }'])

    def test_folder_dashboard_and_explorer(self):
        perms = self.fake.folder_perms[self.cfg.folder_uid]
        self.assertFalse([p for p in perms if p.get("role")])
        dash = self.fake.dashboards[self.cfg.dashboard_uid]
        raw = json.dumps(dash)
        self.assertNotIn("__DS_", raw)
        self.assertIn(self.cfg.ds_prom_uid, raw)
        self.assertEqual(dash["folderUid"], self.cfg.folder_uid)
        for key in ("customer-a", "customer-b"):
            self.assertIn(iso.EXPLORER_ROLE, self.fake.team_roles[self.team(key)["id"]])

    def test_setup_is_idempotent(self):
        before = len(self.fake.teams)
        iso.setup(self.cfg, self.api, out=self.out.append)
        self.assertEqual(len(self.fake.teams), before)

    def test_assign_guards(self):
        say = self.out.append
        self.assertEqual(iso.assign(self.cfg, self.api, "viewer@example.com", "customer-a", out=say), 2)
        self.assertEqual(iso.assign(self.cfg, self.api, "missing@example.com", "customer-a", out=say), 1)
        self.assertEqual(iso.assign(self.cfg, self.api, "user-a@example.com", "customer-a", out=say), 0)
        self.assertEqual(iso.assign(self.cfg, self.api, "user-a@example.com", "customer-b", out=say), 3)
        self.assertEqual(iso.assign(self.cfg, self.api, "user-a@example.com", "customer-a", out=say), 0)
        self.assertEqual(self.fake.members[self.team("customer-a")["id"]], [40])
        with self.assertRaises(iso.ConfigError):
            iso.assign(self.cfg, self.api, "user-b@example.com", "customer-z", out=say)

    def test_verify_passes_then_detects_risks(self):
        iso.assign(self.cfg, self.api, "user-a@example.com", "customer-a", out=self.out.append)
        iso.assign(self.cfg, self.api, "user-b@example.com", "customer-b", out=self.out.append)
        log = []
        self.assertEqual(iso.verify(self.cfg, self.api, out=log.append), 0, "\n".join(log))
        self.assertTrue(any("grafanacloud-prom" in line for line in log))  # unrestricted source reported

        self.fake.users[41]["role"] = "Viewer"                       # role drift
        self.fake.ds_perms[self.cfg.ds_prom_uid].append({"builtInRole": "Viewer", "permission": "Query"})
        log = []
        self.assertEqual(iso.verify(self.cfg, self.api, out=log.append), 1)
        text = "\n".join(log)
        self.assertIn("basic role 'Viewer', must be None", text)
        self.assertIn("basic role Viewer has Query", text)

    def test_verify_flags_team_without_rule(self):
        self.fake.lbac[self.cfg.ds_logs_uid] = [r for r in self.fake.lbac[self.cfg.ds_logs_uid]
                                               if r["teamUid"] != self.team("customer-b")["uid"]]
        log = []
        self.assertEqual(iso.verify(self.cfg, self.api, out=log.append), 1)
        self.assertIn("NO LBAC rule", "\n".join(log))

    def test_tokens_never_printed(self):
        iso.setup(self.cfg, iso.Api(self.server.url, TOKEN, dry_run=True, out=self.out.append), out=self.out.append)
        text = "\n".join(map(str, self.out))
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(ENV["GC_READ_TOKEN"], text)
        self.assertIn("<redacted>", text)

    def test_cleanup(self):
        iso.cleanup(self.cfg, self.api, out=self.out.append)
        self.assertEqual(self.fake.teams, {})
        self.assertNotIn(self.cfg.ds_prom_uid, self.fake.datasources)
        self.assertIn("grafanacloud-prom", self.fake.datasources)  # untouched


class ConfigTest(unittest.TestCase):
    def test_env_file_parsing(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write('# c\nCUSTOMERS="x:Ex Co,y:Why Inc"\nOPERATOR_RULE=\'{ demo="d" }\'\nTEAM_PREFIX=Demo # note\n')
        env = iso.load_env_file(f.name)
        os.unlink(f.name)
        cfg = iso.Config(env)
        self.assertEqual([c["id"] for c in cfg.customers], ["x", "y"])
        self.assertEqual(cfg.rule("operators"), '{ demo="d" }')
        self.assertEqual(cfg.team_name("y"), "Demo - Why Inc")

    def test_env_example_parses_like_shell(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = iso.load_env_file(os.path.join(root, ".env.example"))
        self.assertEqual(env["TEAM_PREFIX"], "Isolation Demo")           # quoted value + trailing comment
        self.assertEqual(env["OPERATOR_RULE"], '{ demo="customer-isolation" }')
        self.assertEqual(env["CUSTOMERS"], "customer-a:Customer A,customer-b:Customer B")
        self.assertEqual(env["GC_WRITE_TOKEN"], "")
        cfg = iso.Config(env)
        self.assertEqual(cfg.team_name("customer-a"), "Isolation Demo - Customer A")

    def test_bad_config(self):
        for spec in ("", "nocolon", "a:A,a:Again", "operators:Ops"):
            with self.assertRaises(iso.ConfigError):
                iso.parse_customers(spec)

    def test_require_rejects_placeholders(self):
        cfg = iso.Config({"GRAFANA_URL": "https://<your-stack>.grafana.net"})
        with self.assertRaises(iso.ConfigError):
            cfg.require("GRAFANA_URL", "GRAFANA_TOKEN")


if __name__ == "__main__":
    unittest.main()
