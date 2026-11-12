"""Client-side licence check. A client installation reports in to the Gilded Age backend, learns whether the
account is switched on, and picks up password-reset requests. Does nothing unless LICENSE_KEY and MASTER_URL are set."""
import json, os, time, urllib.request

VERSION = "gaming-smart 2.0"
STATE = {"ts": 0.0, "active": True, "message": "", "paid_until": "", "ok": None}


def enabled():
    return bool(os.environ.get("LICENSE_KEY") and os.environ.get("MASTER_URL"))


def _post(path, payload):
    req = urllib.request.Request(os.environ["MASTER_URL"].rstrip("/") + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.load(r)


def check(stats_fn, apply_fn, force=False, ttl=300):
    """Cached. If the backend cannot be reached the last known state stays in force (a new install starts switched on)."""
    if not enabled():
        return STATE
    now = time.time()
    if not force and now - STATE["ts"] < ttl:
        return STATE
    STATE["ts"] = now
    try:
        r = _post("/api/license/check", {"key": os.environ["LICENSE_KEY"], "stats": stats_fn(), "version": VERSION})
        STATE.update(active=bool(r.get("active", True)), message=r.get("message", ""), paid_until=r.get("paid_until", ""), ok=True)
        cmds = r.get("commands") or []
        if cmds:
            ids = apply_fn(cmds)
            if ids:
                _post("/api/license/ack", {"key": os.environ["LICENSE_KEY"], "ids": ids})
    except Exception:
        STATE["ok"] = False
    return STATE
