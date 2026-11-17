#!/usr/bin/env python3
"""Synthetic telemetry generator for the customer-isolation demo.

Simulates a Java "publishing-api" serving several demo customers that share ONE
metrics tenant and ONE logs tenant:

  * Metrics: Prometheus exposition on http://127.0.0.1:${DEMO_METRICS_PORT}/metrics
    (scraped by the demo Alloy every 15s and remote-written to Grafana Cloud Metrics).
  * Logs:    structured JSON lines appended to ${DEMO_LOG_DIR}/publishing-api.log
    (tailed by the same Alloy and pushed to Grafana Cloud Logs).

All data is synthetic. Standard library only. Label sets are bounded
(5 routes x 7 statuses per customer).

Customers come from the CUSTOMERS environment variable:
    CUSTOMERS="customer-a:Customer A,customer-b:Customer B"
Odd positions (1st, 3rd, ...) get the "healthy" profile; even positions get the
"recurring incident" profile.
"""

import json
import math
import os
import random
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_CUSTOMERS = "customer-a:Customer A,customer-b:Customer B"

COMMON = {"demo": "customer-isolation", "service_name": "publishing-api", "environment": "demo"}

# (route, method, traffic weight, base median latency seconds)
ROUTES = [
    ("/api/content/{id}", "GET", 0.45, 0.045),
    ("/api/search", "GET", 0.25, 0.120),
    ("/api/media/{id}", "GET", 0.15, 0.030),
    ("/api/content", "POST", 0.10, 0.180),
    ("/api/content/{id}/publish", "PUT", 0.05, 0.260),
]
BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
SECTIONS = ["world", "business", "sports", "tech", "local", "opinion"]

PROFILES = {
    "healthy": {
        "rps": 40.0, "log_success_per_s": 0.8, "err_rate": 0.004, "dbto_rate": 0.0005,
        "lat_mult": 1.0, "heap_max": 4 * 1024**3, "heap_floor": 1.1 * 1024**3,
        "heap_ceiling": 2.6 * 1024**3, "has_incident": False,
    },
    "incident": {
        "rps": 8.0, "log_success_per_s": 0.3, "err_rate": 0.008, "dbto_rate": 0.002,
        "lat_mult": 1.2, "heap_max": 2 * 1024**3, "heap_floor": 0.6 * 1024**3,
        "heap_ceiling": 1.2 * 1024**3, "has_incident": True,
    },
}

LOGGER_API = "com.example.publishing.api.ContentController"
LOGGER_DB = "com.example.publishing.db.ContentRepository"


def parse_customers(spec):
    """Parse "id:Name,id2:Name2" into a list of customer dicts with profiles."""
    customers = []
    for i, item in enumerate(x for x in spec.split(",") if x.strip()):
        if ":" not in item:
            raise ValueError(f"bad CUSTOMERS entry {item!r}; expected id:Name")
        cid, name = (s.strip() for s in item.split(":", 1))
        if not cid or not name or '"' in cid + name:
            raise ValueError(f"bad CUSTOMERS entry {item!r}")
        profile = "healthy" if i % 2 == 0 else "incident"
        customers.append({"customer_id": cid, "customer_name": name, "profile": profile, **PROFILES[profile]})
    if not customers:
        raise ValueError("CUSTOMERS is empty")
    return customers


def in_incident(c, now, cycle_minutes, incident_minutes):
    """True during the last `incident_minutes` of every `cycle_minutes` (wall clock)."""
    if not c["has_incident"]:
        return False
    return (int(now // 60) % cycle_minutes) >= (cycle_minutes - incident_minutes)


class CustomerState:
    def __init__(self, c):
        self.c = c
        self.labels = {**COMMON, "customer_id": c["customer_id"], "customer_name": c["customer_name"]}
        self.requests = {}  # (route, method, status) -> count
        self.hist = {r[0]: {"buckets": [0] * len(BUCKETS), "sum": 0.0, "count": 0} for r in ROUTES}
        self.heap_used = c["heap_floor"]
        self.last_gc_warn = 0.0


def poisson(lam):
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(random.gauss(lam, math.sqrt(lam)) + 0.5))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= random.random()
        if p <= limit:
            return k
        k += 1


def pick_status(method, incident, c):
    err = c["err_rate"] * (15 if incident else 1)
    dbto = c["dbto_rate"] * (40 if incident else 1)
    r = random.random()
    if r < dbto:
        return "503", "db_timeout"
    r -= dbto
    if r < err:
        return "500", "error"
    r -= err
    if r < 0.01:
        return "404", "client"
    r -= 0.01
    if r < 0.004:
        return "400", "client"
    if method == "POST":
        return "201", "ok"
    if method == "GET" and random.random() < 0.08:
        return "304", "ok"
    return "200", "ok"


def now_iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class LogWriter:
    def __init__(self, log_dir, rotate_bytes):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, "publishing-api.log")
        self.rotate_bytes = rotate_bytes
        self.f = open(self.path, "a", buffering=1)

    def write(self, rec):
        self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        if self.f.tell() > self.rotate_bytes:
            self.f.close()
            os.replace(self.path, self.path + ".1")
            self.f = open(self.path, "a", buffering=1)


def base_record(st, ts, level, logger, message, **extra):
    c = st.c
    rec = {
        "timestamp": now_iso(ts),
        "level": level,
        "logger": logger,
        "thread": f"http-nio-8080-exec-{random.randint(1, 48)}",
        "customer_id": c["customer_id"],
        "customer_name": c["customer_name"],
        "service_name": COMMON["service_name"],
        "environment": COMMON["environment"],
        "message": message,
    }
    rec.update(extra)
    return rec


def request_log(st, ts, route, method, status, kind, dur):
    name = st.c["customer_name"]
    article = f"{random.choice(SECTIONS)}-{random.randint(10000, 99999)}"
    common = dict(request_id=uuid.uuid4().hex[:16], http_method=method, route=route,
                  status=int(status), duration_ms=round(dur * 1000, 1))
    if kind == "db_timeout":
        return base_record(
            st, ts, "ERROR", LOGGER_DB,
            f"Database query timed out after 5000ms loading {article} for {name} (pool active=48/50)",
            error_type="java.sql.SQLTimeoutException", db_pool_active=random.randint(46, 50), **common)
    if kind == "error":
        return base_record(
            st, ts, "ERROR", LOGGER_API,
            f"Unhandled exception processing {method} {route} for {name}: failed to render {article}",
            error_type=random.choice(["java.lang.NullPointerException", "java.lang.IllegalStateException",
                                      "org.springframework.web.client.ResourceAccessException"]), **common)
    if kind == "client":
        msg = (f"Content {article} not found for {name}" if status == "404"
               else f"Rejected invalid payload on {method} {route} for {name}: missing field 'headline'")
        return base_record(st, ts, "WARN", LOGGER_API, msg, **common)
    verbs = {
        "/api/content/{id}": f"Served article {article} for {name}",
        "/api/search": f"Search returned {random.randint(0, 50)} results for {name}",
        "/api/media/{id}": f"Served media asset img-{random.randint(1000, 9999)} for {name}",
        "/api/content": f"Created draft {article} for {name}",
        "/api/content/{id}/publish": f"Published article {article} for {name}",
    }
    return base_record(st, ts, "INFO", LOGGER_API, verbs[route], **common)


class Simulator:
    def __init__(self, customers, cycle_minutes=20, incident_minutes=6):
        self.states = [CustomerState(c) for c in customers]
        self.cycle_minutes = cycle_minutes
        self.incident_minutes = incident_minutes
        self.lock = threading.Lock()

    def tick(self, ts, write):
        """Advance one second of simulated traffic; `write` receives log records."""
        for st in self.states:
            c = st.c
            incident = in_incident(c, ts, self.cycle_minutes, self.incident_minutes)
            wobble = 1 + 0.15 * math.sin(ts / 300.0) + random.uniform(-0.05, 0.05)
            total = poisson(c["rps"] * wobble)
            success_log_p = c["log_success_per_s"] / max(total, 1)
            lat_mult = c["lat_mult"] * (4.0 if incident else 1.0)
            logs = []
            with self.lock:
                for _ in range(total):
                    route, method, _w, base = random.choices(ROUTES, weights=[r[2] for r in ROUTES])[0]
                    status, kind = pick_status(method, incident, c)
                    dur = base * lat_mult * random.lognormvariate(0, 0.45)
                    if kind == "db_timeout":
                        dur = 5.0 + random.uniform(0, 0.3)
                    elif kind == "error":
                        dur *= 1.5
                    key = (route, method, status)
                    st.requests[key] = st.requests.get(key, 0) + 1
                    h = st.hist[route]
                    for i, b in enumerate(BUCKETS):
                        if dur <= b:
                            h["buckets"][i] += 1
                    h["sum"] += dur
                    h["count"] += 1
                    if kind != "ok" or random.random() < success_log_p:
                        logs.append((route, method, status, kind, dur))

                # heap: allocation sawtooth with GC; incidents push toward max
                alloc = (c["heap_ceiling"] - c["heap_floor"]) / 45.0
                target_ceiling = c["heap_max"] * 0.96 if incident else c["heap_ceiling"]
                st.heap_used += alloc * random.uniform(0.6, 1.4) * (2.0 if incident else 1.0)
                if st.heap_used >= target_ceiling:
                    st.heap_used = (c["heap_max"] * 0.82 if incident else c["heap_floor"]) * random.uniform(0.97, 1.03)
                heap_used, heap_max = st.heap_used, c["heap_max"]

            for (route, method, status, kind, dur) in logs:
                write(request_log(st, ts, route, method, status, kind, dur))

            heap_pct = heap_used / heap_max
            if heap_pct > 0.85 and ts - st.last_gc_warn > 15:
                st.last_gc_warn = ts
                write(base_record(
                    st, ts, "WARN", "jvm.gc",
                    f"JVM heap pressure for {c['customer_name']}: heap at {heap_pct:.0%} of {heap_max // 1024**2}MiB; "
                    f"G1 Full GC pause {random.randint(400, 1800)}ms",
                    heap_used_bytes=int(heap_used), heap_max_bytes=heap_max, gc="G1 Old Generation"))

    def render_metrics(self):
        def fmt(d):
            return "{" + ",".join(f'{k}="{v}"' for k, v in d.items()) + "}"

        out = ["# HELP publishing_http_requests_total Total HTTP requests handled by publishing-api.",
               "# TYPE publishing_http_requests_total counter"]
        with self.lock:
            for st in self.states:
                for (route, method, status), n in sorted(st.requests.items()):
                    out.append(f"publishing_http_requests_total{fmt({**st.labels, 'route': route, 'method': method, 'status': status})} {n}")
            out += ["# HELP publishing_http_request_duration_seconds HTTP request duration in seconds.",
                    "# TYPE publishing_http_request_duration_seconds histogram"]
            for st in self.states:
                for route, h in st.hist.items():
                    lb = {**st.labels, "route": route}
                    for b, n in zip(BUCKETS, h["buckets"]):
                        out.append(f"publishing_http_request_duration_seconds_bucket{fmt({**lb, 'le': str(b)})} {n}")
                    out.append(f"publishing_http_request_duration_seconds_bucket{fmt({**lb, 'le': '+Inf'})} {h['count']}")
                    out.append(f"publishing_http_request_duration_seconds_sum{fmt(lb)} {h['sum']:.6f}")
                    out.append(f"publishing_http_request_duration_seconds_count{fmt(lb)} {h['count']}")
            out += ["# HELP jvm_memory_used_bytes The amount of used memory.", "# TYPE jvm_memory_used_bytes gauge"]
            for st in self.states:
                out.append(f"jvm_memory_used_bytes{fmt({**st.labels, 'area': 'heap'})} {int(st.heap_used)}")
            out += ["# HELP jvm_memory_max_bytes The maximum amount of memory that can be used.",
                    "# TYPE jvm_memory_max_bytes gauge"]
            for st in self.states:
                out.append(f"jvm_memory_max_bytes{fmt({**st.labels, 'area': 'heap'})} {st.c['heap_max']}")
        return "\n".join(out) + "\n"


def main():
    port = int(os.environ.get("DEMO_METRICS_PORT", "9477"))
    log_dir = os.environ.get("DEMO_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs"))
    rotate = int(os.environ.get("DEMO_LOG_ROTATE_MB", "50")) * 1024 * 1024
    max_hours = float(os.environ.get("DEMO_MAX_HOURS", "168"))  # safety stop; 0 = never
    sim = Simulator(parse_customers(os.environ.get("CUSTOMERS", DEFAULT_CUSTOMERS)),
                    int(os.environ.get("DEMO_INCIDENT_CYCLE_MINUTES", "20")),
                    int(os.environ.get("DEMO_INCIDENT_MINUTES", "6")))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            body = sim.render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    writer = LogWriter(log_dir, rotate)
    started = time.time()
    names = ", ".join(f"{s.c['customer_id']} ({s.c['profile']})" for s in sim.states)
    print(f"generator: metrics on 127.0.0.1:{port}/metrics, logs -> {writer.path}, customers: {names}", flush=True)
    next_t = time.time()
    while not stop.is_set():
        sim.tick(time.time(), writer.write)
        if max_hours and time.time() - started > max_hours * 3600:
            print("generator: DEMO_MAX_HOURS reached, exiting", flush=True)
            break
        next_t += 1.0
        stop.wait(max(0.0, next_t - time.time()))
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
