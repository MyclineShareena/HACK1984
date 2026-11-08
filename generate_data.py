"""Generate the synthetic IncidentVoice scenario into data/ (reproducible, fixed seed).

Scenario: checkout deploy v2.3 (2026-11-05 14:00Z) cut the Postgres connection pool from
50 to 5. Checkout starts timing out on pool checkout and returning HTTP 500s; payments and
inventory see a few aborted downstream requests.

Outputs:
  data/logs/{checkout,payments,inventory}.log   JSON lines
  data/deploys.json, data/services.json
  data/past_incidents/*.md                      15 postmortems (2 about DB pools)
  data/planted_secrets.json                     ground truth of every fake secret planted

All "sensitive" values are fake: example.* domains, 555-01xx phones, test card ranges.

    python generate_data.py
"""
import json
import random
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 1984
ROOT = Path(__file__).resolve().parent / "data"
START = datetime(2026, 11, 5, 13, 0, 0, tzinfo=timezone.utc)
DEPLOY_AT = 3600  # seconds after START -> 14:00Z
WINDOW = 7200

rng = random.Random(SEED)

FIRST = ["maria", "james", "aisha", "wei", "olga", "diego", "priya", "tom", "fatima", "lucas",
         "yuki", "sam", "nina", "omar", "chloe", "raj", "elena", "kofi", "lea", "ivan"]
LAST = ["gonzalez", "smith", "khan", "zhang", "petrova", "rossi", "patel", "nguyen", "haddad",
        "silva", "tanaka", "obrien", "muller", "said", "dubois", "mehta", "novak", "mensah"]
DOMAINS = ["example.com", "example.net", "example.org", "mail.example.com", "shop.test"]


# ---------------------------------------------------------------- fake sensitive values
def fake_email():
    f, l = rng.choice(FIRST), rng.choice(LAST)
    user = rng.choice([f"{f}.{l}", f"{f}{rng.randint(1, 99)}", f"{f[0]}{l}", f"{f}_{l}{rng.randint(70, 99)}"])
    return f"{user}@{rng.choice(DOMAINS)}"


def fake_ip():
    kind = rng.random()
    if kind < 0.4:
        return f"203.0.113.{rng.randint(1, 254)}"
    if kind < 0.6:
        return f"198.51.100.{rng.randint(1, 254)}"
    if kind < 0.8:
        return f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
    return f"{rng.randint(24, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


def fake_card():
    bins = ["4111", "4242", "5555", "5105", "3782", "6011", "4000"]
    b, last4 = rng.choice(bins), f"{rng.randint(0, 9999):04d}"
    style = rng.randrange(5)
    if style == 0:
        return f"{b} **** **** {last4}"
    if style == 1:
        return f"************{last4}"
    if style == 2:
        return f"{b}-XXXX-XXXX-{last4}"
    if style == 3:
        return f"{b}{rng.randint(10, 99)}******{last4}"
    return f"{b} {rng.randint(10, 99)}** **** {last4}"


def _rand(chars, n):
    return "".join(rng.choice(chars) for _ in range(n))


def fake_api_key():
    prefix = rng.choice(["sk_live_"] * 4 + ["rk_live_", "sk_test_"])
    return prefix + _rand(string.ascii_letters + string.digits, rng.choice([24, 32]))


def fake_token():
    b64 = string.ascii_letters + string.digits + "-_"
    if rng.random() < 0.6:  # JWT-shaped
        return "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + _rand(b64, 46) + "." + _rand(b64, 43)
    return _rand("0123456789abcdef", 40)  # opaque


def fake_phone():
    a, n = rng.choice(["415", "212", "617", "312", "206"]), f"01{rng.randint(0, 99):02d}"
    style = rng.randrange(5)
    if style == 0:
        return f"+1-{a}-555-{n}"
    if style == 1:
        return f"({a}) 555-{n}"
    if style == 2:
        return f"{a}.555.{n}"
    if style == 3:
        return f"+1 {a} 555 {n}"
    return f"+44 20 7946 0{rng.randint(100, 999)}"


def plant(entry, planted, kind=None):
    """Insert one fake sensitive value into a log entry (in a field or in the message)."""
    kind = kind or rng.choice(["EMAIL", "IP", "CARD", "API_KEY", "TOKEN", "PHONE"])
    in_msg = rng.random() < 0.45
    if kind == "EMAIL":
        v = fake_email()
        if in_msg:
            entry["msg"] += f"; receipt queued for {v}"
        else:
            entry["customer_email"] = v
    elif kind == "IP":
        v = fake_ip()
        if in_msg:
            entry["msg"] += f" (client {v})"
        else:
            entry["client_ip"] = v
    elif kind == "CARD":
        v = fake_card()
        if in_msg:
            entry["msg"] += f"; card={v}"
        else:
            entry["card"] = v
    elif kind == "API_KEY":
        v = fake_api_key()
        if in_msg:
            entry["msg"] += f"; payment client key={v}"
        else:
            entry["psp_api_key"] = v
    elif kind == "TOKEN":
        v = fake_token()
        if v.startswith("eyJ") and not in_msg and rng.random() < 0.5:
            entry["session_token"] = v
        elif in_msg:
            entry["msg"] += f"; Authorization: Bearer {v}"
        else:
            entry["headers"] = {"authorization": f"Bearer {v}", "user-agent": "checkout-web/5.2"}
    else:
        v = fake_phone()
        if in_msg:
            entry["msg"] += f"; SMS confirmation to {v}"
        else:
            entry["customer_phone"] = v
    planted.append((kind, v))


def ts(sec):
    return (START + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.") + f"{rng.randint(0, 999):03d}Z"


def rid():
    return "req_" + _rand("0123456789abcdef", 12)


def maybe_plant(entry, rate, planted):
    if rng.random() < rate:
        plant(entry, planted)
        if rng.random() < 0.15:
            plant(entry, planted)


# ---------------------------------------------------------------- checkout.log
def checkout_entry(t):
    after = t >= DEPLOY_AT
    since = t - DEPLOY_AT
    p_fail = 0.0 if not after else min(0.75, max(0.0, (since - 90) / 900 * 0.75))
    max_pool = 5 if after else 50
    e = {"ts": None, "level": "INFO", "service": "checkout", "version": "v2.3" if after else "v2.2",
         "request_id": rid()}
    r = rng.random()
    if rng.random() < p_fail:
        kind = rng.randrange(4)
        if kind == 0:
            e.update(level="ERROR", msg="connection pool exhausted: timed out after 30000ms waiting for a connection",
                     error="PoolTimeoutError", db="postgres-primary.internal:5432",
                     pool={"in_use": 5, "max": 5, "waiting": rng.randint(20, 140)})
        elif kind == 1:
            e.update(level="ERROR", msg="POST /api/checkout 500 Internal Server Error", status=500,
                     latency_ms=rng.randint(30000, 30150),
                     error="sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out, timeout 30.00")
        elif kind == 2:
            e.update(level="ERROR", msg="upstream request timeout while reserving inventory", status=504,
                     latency_ms=rng.randint(10000, 10100), upstream="inventory")
        else:
            e.update(level="WARN", msg="db pool saturated", pool={"in_use": 5, "max": 5, "waiting": rng.randint(5, 90)})
    elif r < 0.45:
        lat = rng.randint(70, 240) if not after else rng.randint(400, 9000) if since > 90 else rng.randint(80, 300)
        e.update(msg="POST /api/checkout 200 OK", status=200, latency_ms=lat, order_id=f"ord_{rng.randint(100000, 999999)}")
    elif r < 0.62:
        e.update(msg="GET /api/cart 200 OK", status=200, latency_ms=rng.randint(15, 80))
    elif r < 0.77:
        in_use = rng.randint(4, 22) if not after else 5
        e.update(level="DEBUG", msg="db pool stats", pool={"in_use": in_use, "max": max_pool,
                                                         "waiting": 0 if not after or since < 90 else rng.randint(3, 60)})
    elif r < 0.92:
        e.update(msg="payment authorized", upstream="payments", latency_ms=rng.randint(90, 350))
    else:
        e.update(level="WARN", msg="slow query", query="SELECT ... FROM orders WHERE customer_id = $1",
                 duration_ms=rng.randint(250, 900))
    return e


def build_checkout():
    times = sorted([rng.uniform(0, DEPLOY_AT) for _ in range(140)] +
                   [rng.uniform(DEPLOY_AT + 5, WINDOW) for _ in range(157)])
    rows = []
    for t in times:
        planted = []
        e = checkout_entry(t)
        maybe_plant(e, 0.30, planted)
        rows.append((t, e, planted))
    rows.append((DEPLOY_AT, {"level": "INFO", "service": "checkout", "version": "v2.3",
                             "msg": "deploy started: checkout v2.2 -> v2.3", "deploy_id": "dep_0419", "author": "j.park"}, []))
    rows.append((DEPLOY_AT + 2, {"level": "INFO", "service": "checkout", "version": "v2.3",
                                 "msg": "config loaded", "db": {"pool_max_size": 5, "pool_timeout_s": 30}}, []))
    rows.append((DEPLOY_AT + 4, {"level": "INFO", "service": "checkout", "version": "v2.3",
                                 "msg": "health check passed, rollout 100%"}, []))
    return rows


# ---------------------------------------------------------------- payments / inventory
def build_downstream(service):
    times = sorted(rng.uniform(0, WINDOW) for _ in range(100))
    timeout_idx = set(rng.sample([i for i, t in enumerate(times) if t > DEPLOY_AT + 300], 5))
    rows = []
    for i, t in enumerate(times):
        planted = []
        e = {"ts": None, "level": "INFO", "service": service, "request_id": rid()}
        if i in timeout_idx:
            if service == "payments":
                e.update(level="WARN", msg="client closed request before response (caller: checkout)", status=499,
                         latency_ms=rng.randint(29000, 30500))
            else:
                e.update(level="WARN", msg="reservation released: upstream checkout request aborted (timeout)",
                         status=499, sku=f"SKU-{rng.randint(1000, 9999)}")
        elif service == "payments":
            r = rng.random()
            if r < 0.6:
                e.update(msg="POST /v1/authorize 200 OK", status=200, latency_ms=rng.randint(80, 320),
                         amount_cents=rng.randint(899, 25999), currency="USD")
            elif r < 0.85:
                e.update(level="DEBUG", msg="db pool stats", pool={"in_use": rng.randint(3, 12), "max": 30, "waiting": 0})
            else:
                e.update(msg="webhook delivered", provider="psp", status=200)
        else:
            r = rng.random()
            if r < 0.65:
                e.update(msg="stock reserved", status=200, sku=f"SKU-{rng.randint(1000, 9999)}",
                         qty=rng.randint(1, 4), latency_ms=rng.randint(10, 60))
            elif r < 0.85:
                e.update(msg="GET /v1/stock 200 OK", status=200, latency_ms=rng.randint(5, 40))
            else:
                e.update(level="DEBUG", msg="cache hit ratio", ratio=round(rng.uniform(0.91, 0.99), 3))
        maybe_plant(e, 0.25, planted)
        rows.append((t, e, planted))
    return rows


def write_log(name, rows, truth):
    rows.sort(key=lambda r: r[0])
    path = ROOT / "logs" / f"{name}.log"
    with path.open("w") as f:
        for n, (t, e, planted) in enumerate(rows, 1):
            e["ts"] = ts(t)
            e = {"ts": e.pop("ts"), **e}
            f.write(json.dumps(e) + "\n")
            for kind, value in planted:
                truth.append({"file": f"logs/{name}.log", "line": n, "type": kind, "value": value})
    return len(rows)


# ---------------------------------------------------------------- deploys / services
DEPLOYS = [
    {"version": "v2.0", "service": "checkout", "timestamp": "2026-10-20T16:12:00Z", "author": "m.okafor",
     "summary": "Move checkout to new payments client",
     "config_diff": {"payments.client": {"old": "legacy-http", "new": "psp-sdk-4"}}},
    {"version": "v2.1", "service": "checkout", "timestamp": "2026-10-28T10:40:00Z", "author": "l.ferreira",
     "summary": "Enable address autocomplete flag",
     "config_diff": {"features.address_autocomplete": {"old": False, "new": True}}},
    {"version": "v2.2", "service": "checkout", "timestamp": "2026-11-03T15:05:00Z", "author": "a.singh",
     "summary": "Structured logging + raise inventory timeout",
     "config_diff": {"logging.format": {"old": "text", "new": "json"},
                     "inventory.timeout_ms": {"old": 5000, "new": 10000}}},
    {"version": "v2.3", "service": "checkout", "timestamp": "2026-11-05T14:00:00Z", "author": "j.park",
     "summary": "Reduce idle DB connections to cut Postgres costs",
     "config_diff": {"db.pool.max_size": {"old": 50, "new": 5},
                     "db.pool.idle_timeout_s": {"old": 600, "new": 120}}},
]

SERVICES = {
    "checkout": {"depends_on": ["payments", "inventory", "postgres"], "tier": 1, "owner": "team-checkout"},
    "payments": {"depends_on": ["postgres"], "tier": 1, "owner": "team-payments"},
    "inventory": {"depends_on": [], "tier": 2, "owner": "team-catalog"},
    "postgres": {"depends_on": [], "tier": 0, "owner": "team-platform", "max_connections": 400},
}

# ---------------------------------------------------------------- postmortems
PM = [
    ("INC-2025-014", "Orders API connection pool exhausted after config refactor", "2025-03-11", "orders, postgres", "SEV1",
     "Orders API returned HTTP 500 for ~40% of requests within minutes of a deploy. Logs showed "
     "'timed out waiting for connection from pool' and p99 latency jumped to 30s. Postgres CPU and "
     "connection count were normal; downstream services saw client-closed requests.",
     "A configuration refactor renamed the pool settings and silently changed maximumPoolSize from "
     "40 to 4. Under normal traffic all 4 connections were checked out and requests queued until the "
     "30s pool timeout.",
     "Rolled back to the previous release, restoring pool size 40; errors cleared in 3 minutes. Added "
     "an alert on pool wait time and a CI check that flags pool size changes over 50%."),
    ("INC-2026-003", "Payments DB connection leak exhausts Postgres pool under load", "2026-02-02", "payments, postgres", "SEV2",
     "Payments latency climbed steadily over two hours, followed by 'connection pool exhausted' errors "
     "and failed authorizations. Restarting pods fixed it temporarily.",
     "An error path in the refund handler opened a transaction and never returned the connection to the "
     "pool. Leaked connections accumulated until the pool was empty.",
     "Mitigated by rolling restart. Fixed by wrapping DB sessions in a context manager, setting a pool "
     "leak-detection threshold, and graphing pool in_use / waiting per pod."),
    ("INC-2025-022", "Redis eviction storm causes cart latency spike", "2025-05-19", "cart, redis", "SEV2",
     "Cart page latency rose to 4s and some requests timed out. Redis evicted keys at a high rate.",
     "A marketing batch job wrote large session blobs, pushing Redis past maxmemory with allkeys-lru.",
     "Moved batch job to a separate Redis instance and capped blob size."),
    ("INC-2025-027", "Expired TLS certificate on inventory internal endpoint", "2025-06-30", "inventory", "SEV2",
     "Checkout calls to inventory failed with TLS handshake errors starting at midnight UTC.",
     "Internal certificate was issued manually and not covered by auto-renewal.",
     "Reissued certificate, moved endpoint to cert-manager with renewal alerts."),
    ("INC-2025-031", "DNS resolution failures after CoreDNS upgrade", "2025-07-14", "platform", "SEV1",
     "Intermittent 'no such host' errors across many services; retries partly masked the issue.",
     "New CoreDNS version had a lower cache size and upstream timeout; resolver pods were CPU throttled.",
     "Rolled back CoreDNS, raised limits, added DNS latency SLO."),
    ("INC-2025-035", "Kafka consumer lag on order events after rebalance", "2025-08-08", "orders, kafka", "SEV3",
     "Order confirmation emails delayed by up to 45 minutes; consumer lag graph grew linearly.",
     "A partition rebalance loop was triggered by a consumer with max.poll.interval too low.",
     "Raised max.poll.interval, enabled cooperative rebalancing."),
    ("INC-2025-040", "Disk full on log aggregation node", "2025-09-02", "logging", "SEV3",
     "Logs stopped arriving from several services; dashboards showed gaps.",
     "Debug logging left on after an investigation filled the disk in 6 hours.",
     "Cleared old indices, added disk usage alert at 80%, enforced log level in deploy checks."),
    ("INC-2025-044", "Feature flag rollout broke cart totals", "2025-09-25", "cart, checkout", "SEV2",
     "Customers saw wrong tax amounts at checkout; support tickets spiked.",
     "Flag new_tax_engine was enabled for 100% instead of 5% due to a percentage field typo.",
     "Disabled flag, added two-person review for flag changes above 10%."),
    ("INC-2025-048", "Memory leak in search service causes OOMKilled pods", "2025-10-17", "search", "SEV2",
     "Search pods restarted every ~50 minutes; brief 502s during restarts.",
     "Unbounded in-process cache of query embeddings introduced in a refactor.",
     "Added LRU bound and memory alerts; rolled forward with fix."),
    ("INC-2025-052", "Payment provider outage causes authorization timeouts", "2025-11-21", "payments", "SEV1",
     "Payments to the external PSP timed out after 10s; checkout error rate 25%. Our DB and pools were healthy.",
     "Regional outage at the third-party payment service provider.",
     "Failed over to secondary PSP region; added circuit breaker and status page monitor."),
    ("INC-2025-057", "Schema migration locks orders table", "2025-12-09", "orders, postgres", "SEV1",
     "Writes to orders hung and checkout timed out for 8 minutes during a deploy.",
     "An ALTER TABLE adding a column with a default took an ACCESS EXCLUSIVE lock on a large table.",
     "Killed migration; adopted online migration tooling and lock_timeout for DDL."),
    ("INC-2026-001", "Rate limiter misconfiguration blocks mobile clients", "2026-01-12", "api-gateway", "SEV2",
     "Mobile app users received HTTP 429 on login; web was unaffected.",
     "Rate limit keyed on shared NAT egress IP of a mobile carrier instead of user ID.",
     "Changed key to authenticated user, raised burst limit."),
    ("INC-2026-005", "CDN served stale JavaScript after frontend deploy", "2026-02-20", "web, cdn", "SEV3",
     "Checkout button did nothing for some users after a release.",
     "Asset filenames were not content-hashed, so the CDN cached old bundles referencing removed APIs.",
     "Purged CDN, enabled content-hashed asset names."),
    ("INC-2026-009", "Cron job ran twice and sent duplicate emails", "2026-03-15", "notifications", "SEV3",
     "Customers received duplicate shipping notifications.",
     "Daylight saving time change caused the scheduler to fire the job twice.",
     "Moved schedules to UTC and added idempotency keys."),
    ("INC-2026-012", "Cluster autoscaler failure throttles checkout CPU", "2026-04-02", "checkout, platform", "SEV2",
     "Checkout latency doubled during a sale; CPU throttling high, no errors in application logs.",
     "Autoscaler hit a cloud quota limit and could not add nodes.",
     "Raised quota, added pending-pod alert, pre-scaled before sales."),
]


def write_postmortems():
    d = ROOT / "past_incidents"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.md"):
        old.unlink()
    for iid, title, date, services, sev, symptoms, cause, fix in PM:
        (d / f"{iid}.md").write_text(
            f"# {iid}: {title}\n\n**Date:** {date}  \n**Services:** {services}  \n**Severity:** {sev}\n\n"
            f"## Symptoms\n{symptoms}\n\n## Root cause\n{cause}\n\n## Fix\n{fix}\n")


def main():
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    truth = []
    counts = {
        "checkout": write_log("checkout", build_checkout(), truth),
        "payments": write_log("payments", build_downstream("payments"), truth),
        "inventory": write_log("inventory", build_downstream("inventory"), truth),
    }
    (ROOT / "deploys.json").write_text(json.dumps(DEPLOYS, indent=2) + "\n")
    (ROOT / "services.json").write_text(json.dumps(SERVICES, indent=2) + "\n")
    write_postmortems()
    (ROOT / "planted_secrets.json").write_text(json.dumps(truth, indent=1) + "\n")
    by_type = {}
    for t in truth:
        by_type[t["type"]] = by_type.get(t["type"], 0) + 1
    lines_with = len({(t["file"], t["line"]) for t in truth if t["file"].endswith("checkout.log")})
    print(f"lines: {counts}; checkout lines with secrets: {lines_with}/{counts['checkout']}")
    print(f"planted secrets: {len(truth)} {by_type}; postmortems: {len(PM)}")


if __name__ == "__main__":
    main()
