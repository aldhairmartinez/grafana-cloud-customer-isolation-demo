import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import generator as g  # noqa: E402

LINE = re.compile(r'^([a-z_]+)\{(.*)\} (\S+)$')
LABEL = re.compile(r'([a-z_]+)="([^"]*)"')
COMMON = {"demo": "customer-isolation", "service_name": "publishing-api", "environment": "demo"}


def parse(text):
    series = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = LINE.match(line)
        assert m, f"unparseable exposition line: {line}"
        series.append((m.group(1), dict(LABEL.findall(m.group(2))), float(m.group(3))))
    return series


class GeneratorTest(unittest.TestCase):
    def setUp(self):
        self.customers = g.parse_customers(g.DEFAULT_CUSTOMERS)
        self.sim = g.Simulator(self.customers, cycle_minutes=1, incident_minutes=1)  # always in incident
        self.logs = []
        for t in range(120):
            self.sim.tick(1_700_000_000 + t, self.logs.append)
        self.series = parse(self.sim.render_metrics())

    def test_customer_parsing_and_profiles(self):
        self.assertEqual([c["customer_id"] for c in self.customers], ["customer-a", "customer-b"])
        self.assertEqual([c["profile"] for c in self.customers], ["healthy", "incident"])
        with self.assertRaises(ValueError):
            g.parse_customers("no-colon")
        with self.assertRaises(ValueError):
            g.parse_customers('bad"id:Name')

    def test_every_series_has_the_common_labels(self):
        for name, labels, _ in self.series:
            for k, v in COMMON.items():
                self.assertEqual(labels.get(k), v, f"{name} missing {k}")
            self.assertIn(labels["customer_id"], {"customer-a", "customer-b"})
            self.assertEqual(labels["customer_name"],
                             {"customer-a": "Customer A", "customer-b": "Customer B"}[labels["customer_id"]])
            self.assertNotIn("request_id", labels)

    def test_cardinality_is_bounded(self):
        self.assertLess(len(self.series), 400)
        routes = {l["route"] for n, l, _ in self.series if "route" in l}
        statuses = {l["status"] for n, l, _ in self.series if "status" in l}
        self.assertLessEqual(len(routes), 5)
        self.assertTrue(statuses <= {"200", "201", "304", "400", "404", "500", "503"})

    def test_histogram_is_cumulative(self):
        for cid in ("customer-a", "customer-b"):
            buckets = [(float("inf") if l["le"] == "+Inf" else float(l["le"]), v) for n, l, v in self.series
                       if n == "publishing_http_request_duration_seconds_bucket"
                       and l["customer_id"] == cid and l["route"] == "/api/search"]
            counts = [v for _, v in sorted(buckets)]
            self.assertEqual(counts, sorted(counts), f"{cid} buckets not cumulative")

    def test_profiles_differ(self):
        def total(cid, status=None):
            return sum(v for n, l, v in self.series if n == "publishing_http_requests_total"
                       and l["customer_id"] == cid and (status is None or l["status"].startswith(status)))
        self.assertGreater(total("customer-a"), 2 * total("customer-b"))  # A has more traffic
        self.assertGreater(total("customer-b", "5") / total("customer-b"),
                           2 * total("customer-a", "5") / total("customer-a"))  # B errors more in incident

    def test_logs(self):
        self.assertTrue(self.logs)
        for rec in self.logs:
            for k in ("timestamp", "level", "message", "customer_id", "customer_name", "service_name"):
                self.assertIn(k, rec)
            self.assertIn(rec["customer_name"], rec["message"])  # names appear in text for leak testing
            if rec["logger"] != "jvm.gc":
                self.assertRegex(rec["request_id"], r"^[0-9a-f]{16}$")
        b = [r for r in self.logs if r["customer_id"] == "customer-b"]
        self.assertTrue(any("timed out" in r["message"] for r in b))
        self.assertTrue(any(r["logger"] == "jvm.gc" for r in b))

    def test_incident_window(self):
        healthy, incident = self.customers
        minute = 60
        self.assertFalse(g.in_incident(incident, 13 * minute, 20, 6))
        self.assertTrue(g.in_incident(incident, 14 * minute, 20, 6))
        self.assertTrue(g.in_incident(incident, 19 * minute + 59, 20, 6))
        self.assertFalse(g.in_incident(incident, 20 * minute, 20, 6))
        self.assertFalse(g.in_incident(healthy, 15 * minute, 20, 6))


if __name__ == "__main__":
    unittest.main()
