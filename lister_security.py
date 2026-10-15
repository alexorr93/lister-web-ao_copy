"""
Lister security: owner-only access, authenticator 2FA, permanently trusted devices.

Model (per owner, 10/14):
  * Only the owner account (OWNER_EMAIL) and editor logins can sign in at all.
    Every other business account is locked out (nothing deleted).
  * A NEW device needs password + 6-digit authenticator code once. After that the
    device is trusted forever -- nothing expires. Revoke a device on /security.
  * Editor logins (Grok) stay password-only and keep their Intake-only limits.
  * Lockout: 5 bad passwords or codes within 15 min blocks that email/IP for 15 min.
  * Locked out of your own app (lost phone AND no trusted device left)? Set
    Railway variable LISTER_2FA_BYPASS=1, log in with password, go to /security,
    reset the authenticator, then delete the variable.
"""
import hashlib
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import bcrypt
import pyotp
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

OWNER_EMAIL = (os.getenv("OWNER_EMAIL") or "precisionindustrialmail@gmail.com").strip().lower()
SESSION_COOKIE = "session_id"
DEVICE_COOKIE = "lister_device"
PENDING_COOKIE = "lister_2fa_pending"
FOREVER = 60 * 60 * 24 * 365 * 10          # 10 years = "never expires" for a browser
LOCK_MAX_FAILS = 5
LOCK_WINDOW = 15 * 60

router = APIRouter()
_sb = None
_templates = None
_lock = threading.Lock()


def init(supabase_client, templates):
    global _sb, _templates
    _sb, _templates = supabase_client, templates


def _bypass() -> bool:
    return os.getenv("LISTER_2FA_BYPASS", "").strip() == "1"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_cookie(resp, name, value, max_age=FOREVER):
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=True, samesite="lax")


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, stored: str):
    """Returns (ok, needs_upgrade). Accepts bcrypt and the old salt:sha256 format."""
    if not stored:
        return False, False
    try:
        if stored.startswith("$2"):
            return bcrypt.checkpw(password.encode(), stored.encode()), False
        salt, hashed = stored.split(":", 1)
        ok = secrets.compare_digest(hashlib.sha256((salt + password).encode()).hexdigest(), hashed)
        return ok, ok
    except Exception:
        return False, False


# ------------------------------------------------------------------ lockout
_fails: dict = {}   # key -> [timestamps]


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "?")


def _recent(key):
    cutoff = time.time() - LOCK_WINDOW
    with _lock:
        lst = [t for t in _fails.get(key, []) if t > cutoff]
        _fails[key] = lst
        return lst


def locked_out(*keys) -> int:
    """Seconds remaining on the longest active lock among keys, else 0."""
    worst = 0
    for k in keys:
        lst = _recent(k)
        if len(lst) >= LOCK_MAX_FAILS:
            worst = max(worst, int(lst[-LOCK_MAX_FAILS] + LOCK_WINDOW - time.time()))
    return max(worst, 0)


def record_fail(*keys):
    with _lock:
        for k in keys:
            _fails.setdefault(k, []).append(time.time())


def clear_fails(*keys):
    with _lock:
        for k in keys:
            _fails.pop(k, None)


def lock_message(secs: int) -> str:
    return f"Too many attempts. Try again in {max(1, (secs + 59) // 60)} min."


# ------------------------------------------------------------------ owner / sessions
_owner_cache = {"row": None, "at": 0.0}
_session_cache: dict = {}   # token -> (row or None, fetched_at)


def owner_row(force=False):
    if not force and _owner_cache["row"] and time.time() - _owner_cache["at"] < 60:
        return _owner_cache["row"]
    res = _sb.table("businesses").select("id,email,password_hash,totp_secret,totp_enabled") \
        .eq("email", OWNER_EMAIL).limit(1).execute()
    row = res.data[0] if res.data else None
    _owner_cache.update(row=row, at=time.time())
    return row


def invalidate_caches():
    _owner_cache.update(row=None, at=0.0)
    _session_cache.clear()


def session_row(token: str):
    if not token:
        return None
    hit = _session_cache.get(token)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    try:
        res = _sb.table("sessions").select("token,business_id,role,device_id").eq("token", token).limit(1).execute()
        row = res.data[0] if res.data else None
    except Exception:
        return hit[0] if hit else None
    _session_cache[token] = (row, time.time())
    return row


def create_session(business_id, role, device_id=None) -> str:
    token = secrets.token_hex(32)
    row = {"token": token, "business_id": business_id, "role": role}
    if device_id:
        row["device_id"] = device_id
    _sb.table("sessions").insert(row).execute()
    return token


# ------------------------------------------------------------------ devices
def _guess_device_name(ua: str) -> str:
    ua = ua or ""
    os_name = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else
               "Android" if "Android" in ua else "Windows PC" if "Windows" in ua else
               "Mac" if "Macintosh" in ua else "Linux" if "Linux" in ua else "Device")
    browser = ("Edge" if "Edg/" in ua else "Chrome" if ("Chrome/" in ua or "CriOS" in ua) else
               "Firefox" if ("Firefox/" in ua or "FxiOS" in ua) else "Safari" if "Safari/" in ua else "")
    return f"{os_name} {browser}".strip()


def trusted_device_for(request: Request, business_id):
    tok = request.cookies.get(DEVICE_COOKIE)
    if not tok:
        return None
    res = _sb.table("trusted_devices").select("id,name").eq("business_id", business_id) \
        .eq("token_hash", _sha(tok)).is_("revoked_at", "null").limit(1).execute()
    return res.data[0] if res.data else None


def register_device(request: Request, business_id, name: str):
    tok = secrets.token_urlsafe(32)
    ua = request.headers.get("user-agent", "")[:400]
    res = _sb.table("trusted_devices").insert({
        "business_id": business_id, "token_hash": _sha(tok),
        "name": (name or _guess_device_name(ua))[:80], "user_agent": ua,
        "last_used_at": _now_iso(),
    }).execute()
    return res.data[0]["id"], tok


def touch_device(device_id):
    try:
        _sb.table("trusted_devices").update({"last_used_at": _now_iso()}).eq("id", device_id).execute()
    except Exception:
        pass


def owner_login_response(request: Request, owner) -> RedirectResponse:
    """Password already verified for the owner. Decide: straight in, 2FA, or setup."""
    if _bypass() or not owner.get("totp_enabled"):
        token = create_session(owner["id"], "owner")
        resp = RedirectResponse("/security" if _bypass() else "/security/setup", status_code=302)
        set_cookie(resp, SESSION_COOKIE, token)
        return resp
    dev = trusted_device_for(request, owner["id"])
    if dev:
        touch_device(dev["id"])
        token = create_session(owner["id"], "owner", dev["id"])
        resp = RedirectResponse("/", status_code=302)
        set_cookie(resp, SESSION_COOKIE, token)
        return resp
    pending = secrets.token_urlsafe(24)
    _pending_2fa[pending] = (owner["id"], time.time())
    resp = RedirectResponse("/login/2fa", status_code=302)
    set_cookie(resp, PENDING_COOKIE, pending, max_age=600)
    return resp


_pending_2fa: dict = {}     # pending token -> (business_id, created_at); 10-min challenge only
_pending_setup: dict = {}   # session token -> base32 secret being enrolled


def _pending_business(request: Request):
    tok = request.cookies.get(PENDING_COOKIE, "")
    hit = _pending_2fa.get(tok)
    if not hit or time.time() - hit[1] > 600:
        return None, tok
    return hit[0], tok


# ------------------------------------------------------------------ middleware
# Paths an owner session may use before the authenticator is set up.
_SETUP_ALLOW = ("/security/setup", "/logout", "/login", "/static/", "/favicon.ico")


async def guard(request: Request, call_next):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return await call_next(request)
    path = request.url.path
    if path.startswith(("/static/", "/login")) or path in ("/logout", "/favicon.ico"):
        return await call_next(request)
    try:
        sess = session_row(token)
        owner = owner_row()
    except Exception:
        return await call_next(request)   # DB hiccup: fall through to normal per-route checks
    if not owner:
        return await call_next(request)
    if not sess or str(sess.get("business_id")) != str(owner["id"]):
        # Unknown/revoked session or a non-owner business: treat as signed out.
        resp = (JSONResponse({"detail": "Signed out"}, status_code=401) if path.startswith("/api/")
                else RedirectResponse("/login", status_code=302))
        resp.delete_cookie(SESSION_COOKIE)
        return resp
    if (sess.get("role") or "owner") == "owner" and not owner.get("totp_enabled") and not _bypass():
        if not any(path == p or path.startswith(p) for p in _SETUP_ALLOW):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Finish 2-step setup at /security/setup"}, status_code=403)
            return RedirectResponse("/security/setup", status_code=302)
    return await call_next(request)


def _owner_session(request: Request):
    sess = session_row(request.cookies.get(SESSION_COOKIE, ""))
    owner = owner_row()
    if not sess or not owner or str(sess["business_id"]) != str(owner["id"]) or (sess.get("role") or "owner") != "owner":
        return None, None
    return sess, owner


def _fmt(ts):
    if not ts:
        return "—"
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(ZoneInfo("America/Denver"))
        return dt.strftime("%b %-d, %Y %-I:%M %p")
    except Exception:
        return str(ts)[:16]


# ------------------------------------------------------------------ routes: 2FA at login
@router.get("/login/2fa")
async def login_2fa_page(request: Request, error: str = ""):
    bid, _ = _pending_business(request)
    if not bid:
        return RedirectResponse("/login", status_code=302)
    return _templates.TemplateResponse("login_2fa.html", {
        "request": request, "error": error,
        "device_name": _guess_device_name(request.headers.get("user-agent", "")),
    })


@router.post("/login/2fa")
async def login_2fa_submit(request: Request):
    bid, ptok = _pending_business(request)
    if not bid:
        return RedirectResponse("/login?error=Session+timed+out,+sign+in+again", status_code=302)
    ip = client_ip(request)
    keys = (f"code:{bid}", f"ip:{ip}")
    form = await request.form()
    name = str(form.get("device_name", "")).strip()
    ctx = {"request": request, "device_name": name or _guess_device_name(request.headers.get("user-agent", ""))}
    wait = locked_out(*keys)
    if wait:
        return _templates.TemplateResponse("login_2fa.html", {**ctx, "error": lock_message(wait)})
    code = "".join(ch for ch in str(form.get("code", "")) if ch.isdigit())
    owner = owner_row(force=True)
    if not owner or str(owner["id"]) != str(bid) or not owner.get("totp_secret") \
            or not pyotp.TOTP(owner["totp_secret"]).verify(code, valid_window=1):
        record_fail(*keys)
        return _templates.TemplateResponse("login_2fa.html", {**ctx, "error": "Wrong code. Use the current 6 digits."})
    clear_fails(*keys)
    _pending_2fa.pop(ptok, None)
    device_id, dev_tok = register_device(request, bid, name)
    token = create_session(bid, "owner", device_id)
    resp = RedirectResponse("/", status_code=302)
    set_cookie(resp, SESSION_COOKIE, token)
    set_cookie(resp, DEVICE_COOKIE, dev_tok)
    resp.delete_cookie(PENDING_COOKIE)
    return resp


# ------------------------------------------------------------------ routes: setup
@router.get("/security/setup")
async def setup_page(request: Request, error: str = ""):
    sess, owner = _owner_session(request)
    if not sess:
        return RedirectResponse("/login", status_code=302)
    if owner.get("totp_enabled") and not _bypass():
        return RedirectResponse("/security", status_code=302)
    secret = _pending_setup.get(sess["token"]) or pyotp.random_base32()
    _pending_setup[sess["token"]] = secret
    uri = pyotp.TOTP(secret).provisioning_uri(name=OWNER_EMAIL, issuer_name="Lister AI")
    svg = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2).to_string().decode()
    pretty = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    return _templates.TemplateResponse("security_setup.html", {
        "request": request, "error": error, "qr_svg": svg, "secret": pretty,
        "device_name": _guess_device_name(request.headers.get("user-agent", "")),
    })


@router.post("/security/setup")
async def setup_submit(request: Request):
    sess, owner = _owner_session(request)
    if not sess:
        return RedirectResponse("/login", status_code=302)
    secret = _pending_setup.get(sess["token"])
    if not secret:
        return RedirectResponse("/security/setup", status_code=302)
    form = await request.form()
    code = "".join(ch for ch in str(form.get("code", "")) if ch.isdigit())
    keys = (f"code:{owner['id']}", f"ip:{client_ip(request)}")
    wait = locked_out(*keys)
    if wait:
        return RedirectResponse(f"/security/setup?error={lock_message(wait)}", status_code=302)
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        record_fail(*keys)
        return RedirectResponse("/security/setup?error=Code+didn't+match.+Enter+the+current+6+digits.", status_code=302)
    clear_fails(*keys)
    name = str(form.get("device_name", "")).strip()
    _sb.table("businesses").update({"totp_secret": secret, "totp_enabled": True}).eq("id", owner["id"]).execute()
    _pending_setup.pop(sess["token"], None)
    # This browser becomes trusted device #1; every other owner session is signed
    # out so each of your other devices must pass the code once.
    device_id, dev_tok = register_device(request, owner["id"], name)
    _sb.table("sessions").update({"device_id": device_id}).eq("token", sess["token"]).execute()
    _sb.table("sessions").delete().eq("business_id", owner["id"]).eq("role", "owner") \
        .neq("token", sess["token"]).execute()
    invalidate_caches()
    resp = RedirectResponse("/security", status_code=302)
    set_cookie(resp, DEVICE_COOKIE, dev_tok)
    return resp


# ------------------------------------------------------------------ routes: device management
@router.get("/security")
async def security_page(request: Request):
    sess, owner = _owner_session(request)
    if not sess:
        return RedirectResponse("/login", status_code=302)
    if not owner.get("totp_enabled") and not _bypass():
        return RedirectResponse("/security/setup", status_code=302)
    rows = _sb.table("trusted_devices").select("id,name,created_at,last_used_at") \
        .eq("business_id", owner["id"]).is_("revoked_at", "null").order("created_at").execute().data or []
    this_id = sess.get("device_id")
    devices = [{"id": r["id"], "name": r.get("name") or "Device", "added": _fmt(r.get("created_at")),
                "last": _fmt(r.get("last_used_at")), "this": str(r["id"]) == str(this_id)} for r in rows]
    return _templates.TemplateResponse("security.html", {
        "request": request, "devices": devices, "bypass": _bypass(),
        "enabled": bool(owner.get("totp_enabled")),
    })


def _revoke(business_id, device_ids):
    if not device_ids:
        return
    _sb.table("trusted_devices").update({"revoked_at": _now_iso()}) \
        .eq("business_id", business_id).in_("id", device_ids).execute()
    _sb.table("sessions").delete().eq("business_id", business_id).in_("device_id", device_ids).execute()
    invalidate_caches()


@router.post("/security/devices/{device_id}/revoke")
async def revoke_device(request: Request, device_id: str):
    sess, owner = _owner_session(request)
    if not sess:
        return RedirectResponse("/login", status_code=302)
    _revoke(owner["id"], [device_id])
    return RedirectResponse("/security" if str(device_id) != str(sess.get("device_id")) else "/login", status_code=302)


@router.post("/security/devices/revoke-others")
async def revoke_others(request: Request):
    sess, owner = _owner_session(request)
    if not sess:
        return RedirectResponse("/login", status_code=302)
    rows = _sb.table("trusted_devices").select("id").eq("business_id", owner["id"]) \
        .is_("revoked_at", "null").execute().data or []
    _revoke(owner["id"], [r["id"] for r in rows if str(r["id"]) != str(sess.get("device_id"))])
    # Also drop any stray owner sessions with no device (pre-2FA leftovers).
    _sb.table("sessions").delete().eq("business_id", owner["id"]).eq("role", "owner") \
        .is_("device_id", "null").neq("token", sess["token"]).execute()
    invalidate_caches()
    return RedirectResponse("/security", status_code=302)


@router.post("/security/rename/{device_id}")
async def rename_device(request: Request, device_id: str):
    sess, owner = _owner_session(request)
    if not sess:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    name = str(form.get("name", "")).strip()[:80]
    if name:
        _sb.table("trusted_devices").update({"name": name}).eq("business_id", owner["id"]).eq("id", device_id).execute()
    return RedirectResponse("/security", status_code=302)


@router.post("/security/reset-authenticator")
async def reset_authenticator(request: Request):
    """Start over with a new authenticator (e.g. new phone). Trusted devices stay trusted."""
    sess, owner = _owner_session(request)
    if not sess:
        return RedirectResponse("/login", status_code=302)
    if not sess.get("device_id") and not _bypass():
        return RedirectResponse("/security", status_code=302)
    _sb.table("businesses").update({"totp_enabled": False, "totp_secret": None}).eq("id", owner["id"]).execute()
    invalidate_caches()
    return RedirectResponse("/security/setup", status_code=302)
