"""Durable, provider-independent subscription and licence state (publisher side).

A small SQLite ledger records customers, subscriptions and every billing event
exactly once. Provider events (Stripe sandbox today, any provider later) are
normalised into a handful of transitions. Out-of-order and duplicate events are
safe. Entitlement snapshots match ``licence.FIELDS`` so the customer-side
verifier (``licence.resolve``) can check them offline.

No live payment is created here. Live mode is refused unless explicitly enabled
by the operator after owner approval.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

DAY = 86_400
PAYMENT_GRACE = 7 * DAY
OFFLINE_GRACE = 3 * DAY
SNAPSHOT_TTL = 2 * DAY
PLANS = {  # monthly price in pence; research-backed hypotheses pending owner pricing approval
    "solo": 1500, "pro": 2900, "agency": 7900,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY, email_hash TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id), plan TEXT NOT NULL,
  status TEXT NOT NULL, period_end INTEGER NOT NULL, grace_until INTEGER, cancel_at_period_end INTEGER DEFAULT 0,
  workspaces INTEGER NOT NULL DEFAULT 1, last_event_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, type TEXT NOT NULL, subscription_id TEXT, created INTEGER NOT NULL,
  received_at INTEGER NOT NULL, applied INTEGER NOT NULL, note TEXT, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activations (
  subscription_id TEXT NOT NULL REFERENCES subscriptions(id), device_id TEXT NOT NULL, activated_at INTEGER NOT NULL,
  revoked_at INTEGER, PRIMARY KEY (subscription_id, device_id));
"""


@dataclass(frozen=True)
class Event:
    """Provider-neutral billing event."""
    id: str
    provider: str
    kind: str            # started | trial_started | paid | payment_failed | cancel_scheduled | cancelled | refunded | plan_changed
    subscription_id: str
    customer_id: str
    created: int
    period_end: int | None = None
    plan: str | None = None
    workspaces: int | None = None


class Ledger:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)

    def apply(self, ev: Event, *, now: int | None = None, payload: dict | None = None) -> str:
        """Apply once. Returns 'applied', 'duplicate' or 'stale'."""
        now = now or int(time.time())
        if ev.plan is not None and ev.plan not in PLANS:
            raise ValueError(f"Unknown plan {ev.plan!r}")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM events WHERE id=?", (ev.id,)).fetchone():
                self.db.execute("ROLLBACK")
                return "duplicate"
            sub = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (ev.subscription_id,)).fetchone()
            stale = sub is not None and ev.created < sub["last_event_at"] and ev.kind not in ("refunded",)
            if not stale:
                self._transition(ev, sub, now)
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                            (ev.id, ev.provider, ev.kind, ev.subscription_id, ev.created, now, int(not stale),
                             "older than last applied event" if stale else None, json.dumps(payload or ev.__dict__)))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return "stale" if stale else "applied"

    def _transition(self, ev: Event, sub, now: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO customers VALUES (?,?,?)", (ev.customer_id, None, now))
        if sub is None:
            if ev.kind not in ("started", "trial_started"):
                raise ValueError(f"Event {ev.kind} for unknown subscription {ev.subscription_id}")
            self.db.execute("INSERT INTO subscriptions (id,customer_id,plan,status,period_end,workspaces,last_event_at,"
                            "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                            (ev.subscription_id, ev.customer_id, ev.plan or "solo",
                             "trial" if ev.kind == "trial_started" else "active", ev.period_end or ev.created + 14 * DAY,
                             ev.workspaces or 1, ev.created, now))
            return
        if sub["customer_id"] != ev.customer_id:
            raise ValueError("Event customer does not match subscription owner.")
        sets: dict = {"last_event_at": ev.created, "updated_at": now}
        if ev.kind == "paid":
            sets.update(status="active", grace_until=None, period_end=max(ev.period_end or 0, sub["period_end"]))
        elif ev.kind == "payment_failed":
            if sub["status"] in ("active", "trial"):
                sets.update(status="past_due", grace_until=ev.created + PAYMENT_GRACE)
        elif ev.kind == "cancel_scheduled":
            sets.update(cancel_at_period_end=1)
        elif ev.kind == "cancelled":
            sets.update(status="cancelled", cancel_at_period_end=0,
                        period_end=min(sub["period_end"], ev.period_end or ev.created))
        elif ev.kind == "refunded":
            sets.update(status="refunded", period_end=ev.created)
        elif ev.kind == "plan_changed":
            sets.update(plan=ev.plan or sub["plan"], workspaces=ev.workspaces or sub["workspaces"])
        elif ev.kind in ("started", "trial_started"):
            sets.update(status="active" if ev.kind == "started" else sub["status"])
        else:
            raise ValueError(f"Unsupported event kind {ev.kind}")
        cols = ", ".join(f"{k}=?" for k in sets)
        self.db.execute(f"UPDATE subscriptions SET {cols} WHERE id=?", (*sets.values(), ev.subscription_id))

    def subscription(self, sub_id: str):
        return self.db.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()

    def activate(self, sub_id: str, device_id: str, *, now: int | None = None) -> None:
        """Bind a device; workspaces allowance limits concurrent active devices."""
        now = now or int(time.time())
        sub = self.subscription(sub_id)
        if sub is None:
            raise ValueError("Unknown subscription.")
        active = self.db.execute("SELECT device_id FROM activations WHERE subscription_id=? AND revoked_at IS NULL",
                                 (sub_id,)).fetchall()
        if device_id not in {r["device_id"] for r in active} and len(active) >= sub["workspaces"]:
            raise ValueError("Activation limit reached. Deactivate another device first.")
        self.db.execute("INSERT INTO activations VALUES (?,?,?,NULL) ON CONFLICT DO UPDATE SET revoked_at=NULL",
                        (sub_id, device_id, now))

    def deactivate(self, sub_id: str, device_id: str, *, now: int | None = None) -> None:
        self.db.execute("UPDATE activations SET revoked_at=? WHERE subscription_id=? AND device_id=?",
                        (now or int(time.time()), sub_id, device_id))

    def snapshot(self, sub_id: str, *, now: int | None = None) -> dict:
        """Claims for ``licence.resolve``. Short validity forces periodic refresh while online."""
        now = now or int(time.time())
        sub = self.subscription(sub_id)
        if sub is None:
            raise ValueError("Unknown subscription.")
        status = sub["status"]
        if status == "past_due" and sub["grace_until"] and now < sub["grace_until"]:
            state, until = "active", min(sub["grace_until"], now + SNAPSHOT_TTL)
        elif status in ("active", "trial") and now < sub["period_end"]:
            state, until = status, min(sub["period_end"], now + SNAPSHOT_TTL)
        elif status == "cancelled" and now < sub["period_end"]:
            state, until = "active", min(sub["period_end"], now + SNAPSHOT_TTL)
        elif status in ("active", "trial"):
            state, until = status, now  # period lapsed without payment event: no new access
        else:
            state, until = status, now
        offline = until + OFFLINE_GRACE if state == "active" and until > now else until
        return {"version": 1, "account": sub["customer_id"], "state": state, "issued_at": now,
                "valid_until": until, "offline_until": offline}

    def report(self, *, now: int | None = None) -> dict:
        now = now or int(time.time())
        rows = self.db.execute("SELECT status, plan, COUNT(*) n FROM subscriptions GROUP BY status, plan").fetchall()
        mrr = sum(PLANS[r["plan"]] * r["n"] for r in rows if r["status"] in ("active", "past_due"))
        return {"by_status": {f"{r['status']}/{r['plan']}": r["n"] for r in rows}, "mrr_pence": mrr,
                "events": self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "stale_events": self.db.execute("SELECT COUNT(*) FROM events WHERE applied=0").fetchone()[0]}


# ------------------------------------------------------------- Stripe mapping
_STRIPE_KINDS = {
    "customer.subscription.created": None,  # decided by status
    "invoice.paid": "paid", "invoice.payment_succeeded": "paid",
    "invoice.payment_failed": "payment_failed",
    "customer.subscription.deleted": "cancelled", "charge.refunded": "refunded",
    "customer.subscription.updated": None,
}


def from_stripe(event: dict, *, allow_live: bool = False) -> Event | None:
    """Normalise a verified Stripe event. Returns None for irrelevant event types."""
    if event.get("livemode") and not allow_live:
        raise ValueError("Live-mode events are refused until live billing is approved.")
    kind = _STRIPE_KINDS.get(event.get("type"), "ignore")
    if kind == "ignore":
        return None
    obj = event["data"]["object"]
    meta = obj.get("metadata") or {}
    if event["type"].startswith("customer.subscription"):
        sub_id, customer = obj["id"], obj["customer"]
        if kind is None:
            if event["type"] == "customer.subscription.created":
                kind = "trial_started" if obj.get("status") == "trialing" else "started"
            elif obj.get("cancel_at_period_end"):
                kind = "cancel_scheduled"
            else:
                kind = "plan_changed"
        period_end = obj.get("current_period_end")
    else:
        sub_id, customer = obj.get("subscription") or meta.get("subscription"), obj["customer"]
        period_end = (obj.get("lines", {}).get("data") or [{}])[0].get("period", {}).get("end")
        if not sub_id:
            return None
    return Event(id=event["id"], provider="stripe", kind=kind, subscription_id=sub_id, customer_id=customer,
                 created=int(event["created"]), period_end=period_end, plan=meta.get("plan"),
                 workspaces=int(meta["workspaces"]) if meta.get("workspaces") else None)


def sign(claims: dict, *, private_key: Path, openssl: Path) -> tuple[bytes, bytes]:
    """Ed25519-sign canonical claims with OpenSSL (publisher side)."""
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    with tempfile.TemporaryDirectory() as d:
        src, sig = Path(d) / "claims.json", Path(d) / "sig"
        src.write_bytes(payload)
        subprocess.run([str(openssl), "pkeyutl", "-sign", "-inkey", str(private_key), "-rawin", "-in", str(src),
                        "-out", str(sig)], check=True, capture_output=True, timeout=10)
        return payload, sig.read_bytes()
