"""
Lister AI — FastAPI Web Dashboard
Replaces Streamlit for real-time performance.
"""
import os
import csv
import io
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Body
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from supabase import create_client
from pydantic import BaseModel
from typing import Optional

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
print(f"Connecting to Supabase: {SUPABASE_URL}")
supabase     = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Lister AI")
import os as _os
if _os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def guess_brand_from_title(title: str) -> str:
    first_word = (title or "").split()[0].strip(",.;:-") if title else ""
    return first_word if first_word else "Unbranded"

async def auto_fill_worker():
    """Runs continuously in the background. Any listing missing a brand or eBay category
    gets one filled in automatically within seconds of the scan finishing — no manual click needed."""
    import asyncio

    def needs_category(row: dict) -> bool:
        cat = row.get("ebay_category_id")
        # mantle-scanner writes "0" as a placeholder instead of leaving this NULL,
        # so treat None / "" / "0" all as "not actually categorized yet".
        return cat is None or str(cat).strip() in ("", "0")

    while True:
        try:
            res = supabase.table("listings").select("id,title,brand,ebay_category_id,business_id")\
                .neq("status", "archived")\
                .or_("ebay_category_id.is.null,ebay_category_id.eq.0,ebay_category_id.eq.")\
                .limit(50).execute()
            rows = [r for r in (res.data or []) if needs_category(r)]
            print(f"auto_fill_worker: query returned {len(res.data or [])} row(s), {len(rows)} need a category")
            for row in rows:
                title = row.get("title") or ""
                biz_id = row.get("business_id")
                if not title or title == "Scanning..." or not biz_id:
                    print(f"auto_fill_worker: skipping {row['id']} (title={title!r}, biz_id={biz_id})")
                    continue
                updates = {}
                if not row.get("brand"):
                    updates["brand"] = guess_brand_from_title(title)
                try:
                    suggestion = suggest_ebay_category(title, biz_id, restrict=True)
                    if suggestion:
                        updates["ebay_category_id"] = suggestion["category_id"]
                        print(f"auto_fill_worker: {row['id']} -> category {suggestion['category_id']} ({suggestion.get('name')})")
                    else:
                        fallback = get_ebay_settings(biz_id).get("EBAY_DEFAULT_CATEGORY_ID", "")
                        if fallback:
                            updates["ebay_category_id"] = fallback
                            print(f"auto_fill_worker: {row['id']} -> no B&I match, used fallback category {fallback}")
                        else:
                            print(f"auto_fill_worker: {row['id']} -> NO MATCH and no EBAY_DEFAULT_CATEGORY_ID set for business {biz_id}; will retry next cycle")
                except Exception as e:
                    print(f"auto_fill_worker category error for {row['id']}: {e}")
                if updates:
                    supabase.table("listings").update(updates).eq("id", row["id"]).execute()
        except Exception as e:
            print(f"auto_fill_worker error: {e}")
        await asyncio.sleep(8)

@app.on_event("startup")
async def start_background_jobs():
    import asyncio
    asyncio.create_task(auto_fill_worker())
    asyncio.create_task(order_sync_worker())

async def order_sync_worker():
    """Keeps the local `orders` table fresh automatically, so Financials never has to
    hit eBay/Shopify live. The FIRST time a business is seen, does a full historical
    backfill (365 days) so the table starts complete, not just from whenever this
    feature was turned on. After that, syncs a rolling 14-day window every 20 minutes
    (catches late-arriving fee/refund/tracking data, not just brand-new orders)."""
    import asyncio
    while True:
        try:
            res = supabase.table("app_settings").select("business_id").eq("key", "EBAY_REFRESH_TOKEN").execute()
            business_ids = list(set(r["business_id"] for r in (res.data or [])))
            for biz_id in business_ids:
                try:
                    settings = get_ebay_settings(biz_id)
                    backfilled = settings.get("ORDERS_BACKFILLED", "") == "true"
                    days_back = 14 if backfilled else 720  # ~2 years — eBay's getOrders hard-caps creationdate at 2 years, stricter than Finance API's 5-year limit
                    result = await asyncio.to_thread(sync_orders_for_business, biz_id, days_back)
                    had_errors = bool(result.get("errors"))
                    if not backfilled:
                        if had_errors and result["upserted"] == 0:
                            # Don't mark complete — the eBay pull itself failed, so we got
                            # little/nothing. Retry the full backfill again next cycle instead
                            # of silently settling for an incomplete result.
                            print(f"order_sync_worker: business {biz_id} initial backfill FAILED "
                                  f"(will retry next cycle) — errors: {result['errors']}")
                        else:
                            save_ebay_setting(biz_id, "ORDERS_BACKFILLED", "true")
                            print(f"order_sync_worker: business {biz_id} completed initial backfill "
                                  f"({result['upserted']} order line(s))"
                                  + (f", errors: {result['errors']}" if result.get("errors") else ""))
                    else:
                        print(f"order_sync_worker: business {biz_id} synced {result['upserted']} order line(s)"
                              + (f", errors: {result['errors']}" if result.get("errors") else ""))
                except Exception as e:
                    print(f"order_sync_worker: business {biz_id} failed: {e}")
        except Exception as e:
            print(f"order_sync_worker error: {e}")
        await asyncio.sleep(1200)  # 20 minutes

EBAY_DESCRIPTION = "Shipped primarily with UPS and sometimes USPS. If you have special packing or shipping needs, please send a message. This item is sold in as-is condition. The seller assumes no liability for the use, operation, or installation of this product. Due to the technical nature of this equipment, the buyer is responsible for having the item professionally inspected and installed by a certified technician prior to use."

def photo_url(photo_id: str, thumb: bool = False) -> str:
    if not photo_id or photo_id in ("", "nan", "0"):
        return ""
    if thumb:
        return f"{SUPABASE_URL}/storage/v1/render/image/public/part-photos/{photo_id}?width=500&height=500&resize=cover&quality=80"
    return f"{SUPABASE_URL}/storage/v1/object/public/part-photos/{photo_id}"

def get_all_photo_ids(primary_photo_id: str) -> list:
    """A listing only stores its primary photo_id, but scans capture multiple photos
    per group (group_photos table). Marketplace listings should use ALL of them,
    not just the primary — this looks up the rest via the shared group_id."""
    pid = str(primary_photo_id or "")
    if not pid:
        return []
    try:
        group_row = supabase.table("group_photos").select("group_id").eq("photo_id", pid).limit(1).execute()
        group_id = (group_row.data or [{}])[0].get("group_id", "")
        if not group_id:
            return [pid]
        gp = supabase.table("group_photos").select("photo_id").eq("group_id", group_id).execute()
        all_pids = [r["photo_id"] for r in (gp.data or []) if r.get("photo_id")]
        return all_pids if all_pids else [pid]
    except Exception:
        return [pid]

# ── EBAY INVENTORY API ───────────────────────────────────────── #
EBAY_API_BASE = "https://api.ebay.com"

EBAY_ENV_KEYS = [
    "EBAY_USER_TOKEN", "EBAY_APP_ID", "EBAY_DEV_ID", "EBAY_CERT_ID", "EBAY_RUNAME",
    "EBAY_PAYMENT_POLICY_ID", "EBAY_RETURN_POLICY_ID", "EBAY_FULFILLMENT_POLICY_ID",
    "EBAY_MERCHANT_LOCATION_KEY", "EBAY_LOCATION_ZIP", "EBAY_LOCATION_COUNTRY",
    "EBAY_DEFAULT_CATEGORY_ID",
]

EBAY_OAUTH_SCOPES = (
    "https://api.ebay.com/oauth/api_scope "
    "https://api.ebay.com/oauth/api_scope/sell.inventory "
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment "
    "https://api.ebay.com/oauth/api_scope/sell.finances"
)

def get_ebay_settings(business_id: str) -> dict:
    res = supabase.table("app_settings").select("*").eq("business_id", business_id).execute()
    settings = {row["key"]: row["value"] for row in (res.data or [])}
    return settings

def save_ebay_setting(business_id: str, key: str, value: str):
    existing = supabase.table("app_settings").select("key").eq("business_id", business_id).eq("key", key).limit(1).execute()
    if existing.data:
        supabase.table("app_settings").update({"value": value}).eq("business_id", business_id).eq("key", key).execute()
    else:
        supabase.table("app_settings").insert({"business_id": business_id, "key": key, "value": value}).execute()

def get_ebay_access_token(business_id: str) -> str:
    """Returns a valid eBay access token, transparently refreshing it via the stored
    18-month refresh token if the cached access token is missing or expired.
    Raises a clear error if the account has never completed OAuth."""
    import requests as _req, time, base64 as _b64

    settings = get_ebay_settings(business_id)
    refresh_token = settings.get("EBAY_REFRESH_TOKEN", "")
    access_token = settings.get("EBAY_USER_TOKEN", "")
    expires_at = float(settings.get("EBAY_TOKEN_EXPIRES_AT", "0") or 0)

    if not refresh_token:
        raise Exception("eBay account not connected — go to Settings and click 'Connect eBay Account'")

    # 60s safety margin before expiry
    if access_token and time.time() < (expires_at - 60):
        return access_token

    app_id = settings.get("EBAY_APP_ID", "")
    cert_id = settings.get("EBAY_CERT_ID", "")
    if not app_id or not cert_id:
        raise Exception("Missing EBAY_APP_ID / EBAY_CERT_ID in Settings")

    basic = _b64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    r = _req.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": EBAY_OAUTH_SCOPES,
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise Exception(f"eBay token refresh failed ({r.status_code}): {r.text[:300]} — try reconnecting your eBay account in Settings")

    data = r.json()
    new_token = data["access_token"]
    new_expires_at = time.time() + int(data.get("expires_in", 7200))
    save_ebay_setting(business_id, "EBAY_USER_TOKEN", new_token)
    save_ebay_setting(business_id, "EBAY_TOKEN_EXPIRES_AT", str(new_expires_at))
    return new_token

def ebay_headers(token: str, content_language: bool = True) -> dict:
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_language:
        h["Content-Type"] = "application/json"
        h["Content-Language"] = "en-US"
    return h

def ensure_ebay_location(token: str, location_key: str, zip_code: str, country: str):
    """Create the merchant inventory location if it doesn't already exist. Required before publishing."""
    import requests as _req
    check = _req.get(f"{EBAY_API_BASE}/sell/inventory/v1/location/{location_key}",
                      headers=ebay_headers(token, content_language=False), timeout=15)
    if check.status_code == 200:
        return
    body = {
        "location": {"address": {"postalCode": zip_code, "country": country or "US"}},
        "locationTypes": ["WAREHOUSE"],
        "name": "Primary Location",
    }
    r = _req.post(f"{EBAY_API_BASE}/sell/inventory/v1/location/{location_key}",
                   headers=ebay_headers(token), json=body, timeout=15)
    if r.status_code not in (200, 201, 204):
        raise Exception(f"Failed to create eBay inventory location: {r.status_code} {r.text}")

def ebay_condition(cond: str) -> str:
    return "NEW" if (cond or "").lower() == "new" else "USED_GOOD"

def push_listing_to_ebay(listing: dict, mode: str, hours_from_now: float = None, brand_override: str = None, mpn_override: str = None) -> dict:
    """
    mode: 'draft' | 'now' | 'schedule'
    Returns dict with offer_id, item_id (if published), status, scheduled_at
    """
    import requests as _req
    from datetime import timedelta

    biz_id = listing.get("business_id")
    if not biz_id:
        raise Exception("Listing has no business_id — cannot look up eBay settings safely")
    settings = get_ebay_settings(biz_id)
    token = get_ebay_access_token(biz_id)

    payment_policy    = settings.get("EBAY_PAYMENT_POLICY_ID", "")
    return_policy      = settings.get("EBAY_RETURN_POLICY_ID", "")
    fulfillment_policy = settings.get("EBAY_FULFILLMENT_POLICY_ID", "")
    location_key       = settings.get("EBAY_MERCHANT_LOCATION_KEY", "")
    location_zip       = settings.get("EBAY_LOCATION_ZIP", "")
    location_country   = settings.get("EBAY_LOCATION_COUNTRY", "US")
    category_id        = listing.get("ebay_category_id") or settings.get("EBAY_DEFAULT_CATEGORY_ID", "")

    if not (payment_policy and return_policy and fulfillment_policy and location_key):
        raise Exception("Missing eBay business policy IDs or location key — set these in Settings first")
    if not category_id:
        raise Exception("This item has no eBay category set")

    sku = listing.get("ebay_sku") or f"lister-{listing['id']}"
    title = (listing.get("title") or "Untitled item")[:80]
    desc  = listing.get("description") or EBAY_DESCRIPTION
    qty   = int(listing.get("quantity") or 1)
    price = float(listing.get("price") or 0)
    pid   = str(listing.get("photo_id") or "")
    images = [photo_url(p) for p in get_all_photo_ids(pid) if photo_url(p)] if pid else []

    # 1. Create/replace inventory item
    brand = (brand_override or listing.get("brand") or "").strip()
    if not brand:
        first_word = title.split()[0].strip(",.;:-") if title else ""
        brand = first_word if first_word else "Unbranded"

    # Guess MPN: the longest alphanumeric (letters+digits) token in the title that isn't the brand —
    # usually the true part number rather than a shorter model class label.
    # eBay requires SOME MPN value in many categories — if we can't guess one, send their
    # standard "Does Not Apply" placeholder rather than omitting the aspect entirely, since
    # a missing BrandMPN aspect makes publishOffer fail outright in those categories.
    mpn = (mpn_override or "").strip() or None
    mpn_is_fallback = False
    if not mpn:
        alnum_tokens = [
            w.strip(",.;:-") for w in title.split()
            if w.lower().strip(",.;:-") != brand.lower()
            and any(c.isdigit() for c in w) and any(c.isalpha() for c in w)
        ]
        if alnum_tokens:
            mpn = max(alnum_tokens, key=len)
    if not mpn:
        mpn = "Does Not Apply"
        mpn_is_fallback = True

    product_data = {
        "title": title,
        "description": desc,
        "imageUrls": images,
        "aspects": {"Brand": [brand], "MPN": [mpn]},
        "brand": brand,
        "mpn": mpn,
    }

    inv_body = {
        "condition": ebay_condition(listing.get("condition")),
        "product": product_data,
        "availability": {"shipToLocationAvailability": {"quantity": qty}},
    }
    r = _req.put(f"{EBAY_API_BASE}/sell/inventory/v1/inventory_item/{sku}",
                 headers=ebay_headers(token), json=inv_body, timeout=20)
    if r.status_code not in (200, 201, 204):
        raise Exception(f"createInventoryItem failed: {r.status_code} {r.text}")

    ensure_ebay_location(token, location_key, location_zip, location_country)

    # 2. Create offer (or reuse existing offer_id if already drafted)
    offer_id = listing.get("ebay_offer_id")
    scheduled_at_iso = None
    offer_body = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": qty,
        "categoryId": str(category_id),
        "listingDescription": desc,
        "listingPolicies": {
            "paymentPolicyId": payment_policy,
            "returnPolicyId": return_policy,
            "fulfillmentPolicyId": fulfillment_policy,
            "bestOfferTerms": {"bestOfferEnabled": True},  # always on, per your standing preference
        },
        "pricingSummary": {"price": {"value": f"{price:.2f}", "currency": "USD"}},
        "merchantLocationKey": location_key,
    }
    if mode == "schedule":
        hrs = float(hours_from_now or 1)
        scheduled_dt = datetime.utcnow() + timedelta(hours=hrs)
        scheduled_at_iso = scheduled_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        offer_body["listingStartDate"] = scheduled_at_iso

    if offer_id:
        r = _req.put(f"{EBAY_API_BASE}/sell/inventory/v1/offer/{offer_id}",
                      headers=ebay_headers(token), json=offer_body, timeout=20)
        if r.status_code not in (200, 204):
            raise Exception(f"updateOffer failed: {r.status_code} {r.text}")
    else:
        r = _req.post(f"{EBAY_API_BASE}/sell/inventory/v1/offer",
                       headers=ebay_headers(token), json=offer_body, timeout=20)
        if r.status_code == 400 and "already exists" in r.text.lower():
            # A previous attempt for this SKU already created an offer on eBay's side
            # (e.g. it died at the publish step afterward) but our DB never learned the
            # offer_id. eBay's error body includes it — recover it and update instead of failing.
            existing_offer_id = None
            try:
                for err in r.json().get("errors", []):
                    for p in err.get("parameters", []):
                        if p.get("name") == "offerId":
                            existing_offer_id = p.get("value")
            except Exception:
                pass
            if existing_offer_id:
                offer_id = existing_offer_id
                r2 = _req.put(f"{EBAY_API_BASE}/sell/inventory/v1/offer/{offer_id}",
                               headers=ebay_headers(token), json=offer_body, timeout=20)
                if r2.status_code not in (200, 204):
                    raise Exception(f"updateOffer (recovered offerId) failed: {r2.status_code} {r2.text}")
            else:
                raise Exception(f"createOffer failed: {r.status_code} {r.text}")
        elif r.status_code not in (200, 201):
            raise Exception(f"createOffer failed: {r.status_code} {r.text}")
        else:
            offer_id = r.json().get("offerId")

    result = {"offer_id": offer_id, "sku": sku, "item_id": None, "status": "draft", "scheduled_at": None, "brand": brand, "mpn": mpn, "mpn_is_fallback": mpn_is_fallback}

    if mode == "draft":
        return result

    # 3. Publish
    r = _req.post(f"{EBAY_API_BASE}/sell/inventory/v1/offer/{offer_id}/publish",
                   headers=ebay_headers(token, content_language=False), timeout=20)
    if r.status_code not in (200, 201):
        raise Exception(f"publishOffer failed: {r.status_code} {r.text}")
    listing_id = r.json().get("listingId")
    result["item_id"] = listing_id
    result["status"] = "scheduled" if mode == "schedule" else "published"
    result["scheduled_at"] = scheduled_at_iso
    return result

# ── PAGES ─────────────────────────────────────────────────────── #

@app.get("/auction/research", response_class=HTMLResponse)
async def auction_research_page(request: Request):
    import os
    with open(os.path.join(os.path.dirname(__file__), "templates", "auction_research.html")) as f:
        html = f.read()
    return HTMLResponse(content=html, headers={
        "Content-Security-Policy": "default-src * blob: data:; script-src * blob: data: 'unsafe-inline' 'unsafe-eval'; style-src * 'unsafe-inline'; img-src * blob: data:;"
    })

@app.get("/auction", response_class=HTMLResponse)
async def auction_page(request: Request):
    business_id = require_auth(request)
    if not business_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)
    import os
    with open(os.path.join(os.path.dirname(__file__), "templates", "auction.html")) as f:
        html = f.read()
    return HTMLResponse(content=html, headers={
        "Content-Security-Policy": "default-src * blob: data:; script-src * blob: data: 'unsafe-inline' 'unsafe-eval'; style-src * 'unsafe-inline'; img-src * blob: data:;"
    })

@app.get("/v2", response_class=HTMLResponse)
async def dashboard_v2(request: Request):
    from fastapi.responses import HTMLResponse
    import os
    with open(os.path.join(os.path.dirname(__file__), "templates", "v2.html")) as f:
        html = f.read()
    return HTMLResponse(content=html, headers={
        "Content-Security-Policy": "default-src * blob: data:; script-src * blob: data: 'unsafe-inline' 'unsafe-eval'; style-src * 'unsafe-inline'; img-src * blob: data:;"
    })

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    business_id, is_admin = get_business_info(request)
    if not business_id or not is_admin:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)
    with open(os.path.join(os.path.dirname(__file__), "templates", "admin.html")) as f:
        return HTMLResponse(f.read())

@app.patch("/api/admin/businesses/{business_id}")
async def admin_update_business(business_id: str, request: Request):
    auth_business_id, is_admin = get_business_info(request)
    if not auth_business_id or not is_admin:
        raise HTTPException(401, "Unauthorized")
    try:
        body = await request.json()
        allowed = {k: v for k, v in body.items() if k in ("scan_limit", "scan_count", "is_admin")}
        supabase.table("businesses").update(allowed).eq("id", business_id).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.delete("/api/admin/businesses/{business_id}")
async def admin_delete_business(business_id: str, request: Request):
    auth_business_id, is_admin = get_business_info(request)
    if not auth_business_id or not is_admin:
        raise HTTPException(401, "Unauthorized")
    try:
        supabase.table("sessions").delete().eq("business_id", business_id).execute()
        supabase.table("listings").delete().eq("business_id", business_id).execute()
        supabase.table("listing_groups").delete().eq("business_id", business_id).execute()
        supabase.table("businesses").delete().eq("id", business_id).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/admin/businesses")
async def admin_businesses(request: Request):
    auth_business_id, is_admin = get_business_info(request)
    if not auth_business_id or not is_admin:
        raise HTTPException(401, "Unauthorized")
    try:
        biz = supabase.table("businesses").select("id,name,email,created_at,scan_count,scan_limit,is_admin").order("created_at", desc=True).execute()
        businesses = biz.data or []
        for b in businesses:
            lid = supabase.table("listings").select("id", count="exact").eq("business_id", b["id"]).execute()
            b["listing_count"] = lid.count or 0
            sid = supabase.table("auction_research_sessions").select("id,created_at", count="exact").eq("business_id", b["id"]).order("created_at", desc=True).limit(1).execute()
            b["scan_count"] = sid.count or 0
            b["last_active"] = sid.data[0]["created_at"] if sid.data else None
        return {"businesses": businesses}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/paywall", response_class=HTMLResponse)
async def paywall_page(request: Request):
    with open(os.path.join(os.path.dirname(__file__), "templates", "paywall.html")) as f:
        return HTMLResponse(f.read())

@app.get("/team", response_class=HTMLResponse)
async def team_portal(request: Request):
    with open(os.path.join(os.path.dirname(__file__), "templates", "team_portal.html")) as f:
        return HTMLResponse(f.read())

@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request):
    import os
    with open(os.path.join(os.path.dirname(__file__), "templates", "portal.html")) as f:
        html = f.read()
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html, headers={
        "Content-Security-Policy": "default-src * blob: data:; script-src * blob: data: 'unsafe-inline' 'unsafe-eval'; style-src * 'unsafe-inline'; img-src * blob: data:;"
    })


def require_auth(request: Request):
    """Returns business_id if authenticated, else None."""
    token = request.cookies.get("session_id")
    if not token:
        return None
    try:
        res = supabase.table("sessions").select("business_id").eq("token", token).execute()
        if res.data:
            return res.data[0]["business_id"]
    except Exception:
        pass
    return None

def get_business_info(request: Request):
    """Returns (business_id, is_admin) or (None, False)."""
    token = request.cookies.get("session_id")
    if not token:
        return None, False
    try:
        res = supabase.table("sessions").select("business_id").eq("token", token).execute()
        if not res.data:
            return None, False
        bid = res.data[0]["business_id"]
        biz = supabase.table("businesses").select("is_admin").eq("id", bid).execute()
        is_admin = bool(biz.data[0]["is_admin"]) if biz.data else False
        return bid, is_admin
    except Exception:
        pass
    return None, False

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    business_id, is_admin = get_business_info(request)
    if not business_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)
    biz = supabase.table("businesses").select("name,email").eq("id", business_id).limit(1).execute()
    account_label = ""
    if biz.data:
        account_label = biz.data[0].get("email") or biz.data[0].get("name") or ""
    return templates.TemplateResponse("index.html", {"request": request, "is_admin": is_admin, "account_label": account_label})

# ── API: LISTINGS ─────────────────────────────────────────────── #

@app.get("/api/listings")
async def get_listings(request: Request, archived: bool = False):
    business_id = require_auth(request)
    if not business_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        q = supabase.table("listings").select("*").eq("business_id", business_id)
        q = q.eq("status", "archived") if archived else q.neq("status", "archived")
        res = q.order("created_at", desc=True).execute()
        listings = res.data or []

        # Batch fetch all group photos for these listings
        primary_pids = [str(l.get("photo_id") or "") for l in listings if l.get("photo_id")]
        group_photo_map = {}  # photo_id -> [all photo_ids in same group]
        if primary_pids:
            try:
                gp_res = supabase.table("group_photos")                    .select("group_id, photo_id")                    .in_("photo_id", primary_pids[:100])                    .execute()
                # Map primary photo -> group_id
                pid_to_gid = {row["photo_id"]: row["group_id"] for row in (gp_res.data or [])}
                group_ids = list(set(pid_to_gid.values()))
                if group_ids:
                    all_gp = supabase.table("group_photos")                        .select("group_id, photo_id")                        .in_("group_id", group_ids)                        .execute()
                    # Build group_id -> [photo_ids]
                    gid_to_photos = {}
                    for row in (all_gp.data or []):
                        gid_to_photos.setdefault(row["group_id"], []).append(row["photo_id"])
                    # Map primary photo_id -> all photos in its group
                    for pid, gid in pid_to_gid.items():
                        group_photo_map[pid] = gid_to_photos.get(gid, [pid])
            except Exception as search_err:
                print(f"   Search grounding failed: {search_err}")


        # Batch-fetch readable category paths for any listings that have a category set
        cat_ids = list({str(l["ebay_category_id"]) for l in listings if l.get("ebay_category_id")})
        cat_path_map = {}
        if cat_ids:
            try:
                cat_res = supabase.table("ebay_categories").select("category_id,path").in_("category_id", cat_ids[:200]).execute()
                cat_path_map = {row["category_id"]: row["path"] for row in (cat_res.data or [])}
            except Exception as e:
                print(f"category path lookup failed: {e}")

        default_cat_id = str(get_ebay_settings(business_id).get("EBAY_DEFAULT_CATEGORY_ID", "") or "")

        for l in listings:
            pid = str(l.get("photo_id") or "")
            all_photos = group_photo_map.get(pid, [pid] if pid else [])
            l["thumb_url"]  = photo_url(pid, thumb=True)
            l["full_url"]   = photo_url(pid)
            l["all_photos"] = [{"thumb": photo_url(p, thumb=True), "full": photo_url(p)} for p in all_photos if p]
            l["ebay_category_path"] = cat_path_map.get(str(l.get("ebay_category_id") or ""), "")
            l["ebay_category_is_default"] = bool(default_cat_id) and str(l.get("ebay_category_id") or "") == default_cat_id
            # Coerce types
            l["price"]      = float(l.get("price") or 0)
            l["price_used"] = float(l.get("price_used") or 0)
            l["price_new"]  = float(l.get("price_new") or 0)
            l["quantity"]   = int(l.get("quantity") or 1)
            # Normalize condition — default to "used" if blank/null
            cond = str(l.get("condition") or "").strip().lower()
            l["condition"] = cond if cond in ("new", "used") else "used"
            # Normalize listing_type — items from batch upload are not auctions
            lt = str(l.get("listing_type") or "").strip().lower()
            if lt not in ("auction", "fixed"):
                l["listing_type"] = "fixed"
        return JSONResponse(listings)
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(500, str(e))

class UpdateField(BaseModel):
    field: str
    value: object



@app.get("/api/export/ebay-csv")
async def export_ebay_csv(request: Request):
    import csv, io
    from fastapi.responses import StreamingResponse
    from datetime import datetime
    business_id = require_auth(request)
    try:
        q = supabase.table("listings").select(
            "title,description,price,price_used,price_new,quantity,condition,photo_id,ebay_category_id,description"
        ).neq("status", "archived")
        if business_id:
            q = q.eq("business_id", business_id)
        res = q.execute()
        items = res.data or []
    except Exception as e:
        raise HTTPException(500, str(e))

    output = io.StringIO()

    # eBay draft flat file headers — same format as the working version
    output.write('#INFO,Version=0.0.2,Template= eBay-draft-listings-template_US,,,,,,,,\n')
    output.write('#INFO Action and Category ID are required fields.,,,,,,,,,,\n')
    output.write('#INFO,,,,,,,,,,\n')
    output.write('Action(SiteID=US|Country=US|Currency=USD|Version=1193|CC=UTF-8),Custom label (SKU),Category ID,Title,UPC,Price,Quantity,Item photo URL,Condition ID,Description,Format\n')

    writer = csv.writer(output, quoting=csv.QUOTE_ALL)

    for item in items:
        cond = str(item.get("condition") or "used").strip().lower()
        cond_id = "NEW" if cond == "new" else "USED"
        pid = str(item.get("photo_id") or "")
        # Look up all photos for this listing via group_photos table
        try:
            _gp = supabase.table("group_photos").select("photo_id").eq("group_id",
                (supabase.table("group_photos").select("group_id").eq("photo_id", pid).execute().data or [{}])[0].get("group_id", "")
            ).execute()
            _all_pids = [r["photo_id"] for r in (_gp.data or [])] if _gp.data else [pid]
            pic = "|".join(photo_url(p) for p in _all_pids if p) if _all_pids else (photo_url(pid) if pid else "")
        except Exception:
            pic = photo_url(pid) if pid else ""
        category_id = "12576"
        price = float(item.get("price") or item.get("price_used") or 0)
        writer.writerow([
            "Draft",
            "",
            category_id,
            str(item.get("title",""))[:80],
            "",
            f"{price:.2f}",
            str(int(item.get("quantity") or 1)),
            pic,
            cond_id,
            str(item.get('description','') or EBAY_DESCRIPTION),
            "FixedPrice",
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    fn = f"ebay_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fn}"}
    )


@app.get("/api/photos/view/{photo_id}")
async def view_photo(photo_id: str, t: str = ""):
    from fastapi.responses import Response
    img_bytes = supabase.storage.from_("part-photos").download(photo_id)
    return Response(content=img_bytes, media_type="image/jpeg", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0"
    })

@app.post("/api/photos/rotate")
async def rotate_photo(request: Request):
    from PIL import Image
    import io
    body = await request.json()
    photo_id = body.get("photo_id", "")
    if not photo_id:
        raise HTTPException(400, "photo_id required")
    try:
        img_bytes = supabase.storage.from_("part-photos").download(photo_id)
        img = Image.open(io.BytesIO(img_bytes))
        direction = body.get('direction', 'cw')
        img = img.rotate(90 if direction == 'ccw' else -90, expand=True)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        buf.seek(0)
        supabase.storage.from_("part-photos").upload(
            path=photo_id,
            file=buf.read(),
            file_options={"content-type": "image/jpeg", "upsert": "true"}
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/reset-queue")
async def reset_queue():
    try:
        supabase.table("listing_groups").update({"status": "waiting"}).in_("status", ["processing", "pending"]).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/stuck-groups/{group_id}/clear")
async def clear_stuck_group(group_id: str, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        group_res = supabase.table("listing_groups").select("created_at").eq("id", group_id).limit(1).execute()
        created_at = group_res.data[0]["created_at"] if group_res.data else None

        # Stop it from ever being retried or counted as "processing" again
        supabase.table("listing_groups").update({"status": "archived"}).eq("id", group_id).eq("business_id", business_id).execute()

        # If a listing already exists for this group's photo, archive it. If not (scanning never
        # got far enough to create one), create a minimal archived placeholder so it's actually
        # visible on the Archive page instead of silently vanishing.
        gp = supabase.table("group_photos").select("photo_id").eq("group_id", group_id).limit(1).execute()
        if gp.data:
            pid = gp.data[0]["photo_id"]
            title = "Failed scan (unknown date)"
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    title = f"Failed scan — {dt.strftime('%b %d, %Y %I:%M %p')}"
                except Exception:
                    pass

            existing = supabase.table("listings").select("id,title").eq("photo_id", pid).eq("business_id", business_id).limit(1).execute()
            if existing.data:
                update = {"status": "archived"}
                cur_title = (existing.data[0].get("title") or "").strip()
                if not cur_title or cur_title == "Scanning...":
                    update["title"] = title
                supabase.table("listings").update(update).eq("id", existing.data[0]["id"]).execute()
            else:
                supabase.table("listings").insert({
                    "business_id": business_id,
                    "photo_id": pid,
                    "title": title,
                    "status": "archived",
                    "price": 0,
                    "quantity": 1,
                    "condition": "used",
                }).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/stuck-groups")
async def get_stuck_groups(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        groups = supabase.table("listing_groups").select("id,status,created_at")\
            .eq("business_id", business_id).in_("status", ["pending", "processing"]).execute()
        out = []
        for g in (groups.data or []):
            gp = supabase.table("group_photos").select("photo_id").eq("group_id", g["id"]).limit(1).execute()
            pid = gp.data[0]["photo_id"] if gp.data else ""
            out.append({
                "group_id": g["id"],
                "status": g["status"],
                "created_at": g.get("created_at"),
                "thumb_url": photo_url(pid, thumb=True) if pid else "",
            })
        return {"stuck": out}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/stats")
async def get_stats(request: Request):
    try:
        business_id = require_auth(request)
        if not business_id:
            return {"total": 0, "processing": 0, "value": 0}
        res = supabase.table("listings").select("price, status").eq("business_id", business_id).neq("status", "archived").execute()
        items = res.data or []
        total = len(items)
        value = sum(float(i.get("price") or 0) for i in items)
        pending = supabase.table("listing_groups").select("id").eq("business_id", business_id).in_("status", ["pending", "processing"]).execute()
        processing = len(pending.data or [])
        return {"total": total, "processing": processing, "value": round(value, 2)}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.patch("/api/listings/{item_id}")
async def update_listing(item_id: str, body: UpdateField, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        supabase.table("listings").update({body.field: body.value}).eq("id", item_id).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

class EbaySubmit(BaseModel):
    mode: str  # 'draft' | 'now' | 'schedule'
    hours_from_now: Optional[float] = None
    brand: Optional[str] = None
    mpn: Optional[str] = None

@app.post("/api/listings/{item_id}/ebay")
async def submit_to_ebay(item_id: str, body: EbaySubmit, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        res = supabase.table("listings").select("*").eq("id", item_id).limit(1).execute()
        if not res.data:
            raise HTTPException(404, "Listing not found")
        listing = res.data[0]
        result = push_listing_to_ebay(listing, body.mode, body.hours_from_now, body.brand, body.mpn)
        update = {
            "ebay_offer_id": result["offer_id"],
            "ebay_sku": result["sku"],
            "ebay_status": result["status"],
            "ebay_error": None,
            "brand": result.get("brand"),
        }
        if result["item_id"]:
            update["ebay_item_id"] = result["item_id"]
        if result["scheduled_at"]:
            update["ebay_scheduled_at"] = result["scheduled_at"]
        try:
            update["ebay_mpn"] = result.get("mpn")
            update["ebay_mpn_is_fallback"] = result.get("mpn_is_fallback", False)
            supabase.table("listings").update(update).eq("id", item_id).execute()
        except Exception:
            # ebay_mpn / ebay_mpn_is_fallback columns may not exist on this Supabase project yet —
            # retry without them so the actual eBay submission still gets saved
            update.pop("ebay_mpn", None)
            update.pop("ebay_mpn_is_fallback", None)
            supabase.table("listings").update(update).eq("id", item_id).execute()
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        supabase.table("listings").update({"ebay_status": "failed", "ebay_error": str(e)}).eq("id", item_id).execute()
        raise HTTPException(500, str(e))

def get_gemini_key(business_id: str) -> str:
    settings = get_ebay_settings(business_id)
    return settings.get("GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")

def sync_ebay_categories(token: str) -> int:
    """Download eBay's full category tree once and save it locally — EVERY node, not just leaves,
    so parent/grouping categories show their real IDs too, not just listable leaf categories.
    is_leaf marks which ones are actually valid for listing an item (eBay requires a leaf category)."""
    import requests as _req
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    r = _req.get("https://api.ebay.com/commerce/taxonomy/v1/category_tree/0", headers=headers, timeout=60)
    if r.status_code != 200:
        raise Exception(f"getCategoryTree failed: {r.status_code} {r.text}")
    tree = r.json()

    all_nodes = []
    def walk(node, path):
        cat = node.get("category", {})
        name = cat.get("categoryName", "")
        cid = cat.get("categoryId", "")
        new_path = path + [name] if name else path
        children = node.get("childCategoryTreeNodes", [])
        if cid:
            all_nodes.append({
                "category_id": str(cid), "name": name, "path": " > ".join(new_path),
                "is_leaf": not bool(children),
            })
        for child in children:
            walk(child, new_path)

    root = tree.get("rootCategoryNode", {})
    for child in root.get("childCategoryTreeNodes", []):
        walk(child, [])

    # Upsert in batches so we don't blow request size limits
    for i in range(0, len(all_nodes), 500):
        batch = all_nodes[i:i+500]
        supabase.table("ebay_categories").upsert(batch, on_conflict="category_id").execute()
    return len(all_nodes)

def suggest_ebay_category(title: str, business_id: str, restrict: bool = True, exclude_id: str = None) -> dict:
    """Match an item title to the best eBay leaf category using eBay's OWN live category-suggestion
    engine (same one that correctly found 'Other Business & Industrial' = 26261) — this is far more
    accurate than local keyword matching, since it's eBay's real algorithm trained on real listings.
    restrict=True limits results to Business & Industrial / eBay Motors (your usual categories).
    exclude_id lets a repeat click skip the previous pick and try the next-best suggestion."""
    import requests as _req
    try:
        token = get_ebay_access_token(business_id)
    except Exception as e:
        print(f"suggest_ebay_category: {e}")
        return {}

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    r = _req.get(
        "https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/get_category_suggestions",
        headers=headers, params={"q": title}, timeout=15
    )
    if r.status_code != 200:
        print(f"suggest_ebay_category: eBay API returned {r.status_code}: {r.text[:500]}")
        return {}
    data = r.json()

    results = []
    for s in data.get("categorySuggestions", []):
        cat = s.get("category", {})
        ancestors = s.get("categoryTreeNodeAncestors", [])
        path = " > ".join(a.get("categoryName", "") for a in ancestors[::-1])
        full_path = f"{path} > {cat.get('categoryName','')}" if path else cat.get("categoryName", "")
        results.append({"category_id": cat.get("categoryId"), "name": cat.get("categoryName"), "path": full_path})

    if restrict:
        results = [r for r in results if "Business & Industrial" in r["path"] or "eBay Motors" in r["path"]]
    if exclude_id:
        results = [r for r in results if r["category_id"] != exclude_id]

    return results[0] if results else {}

@app.post("/api/ebay/sync-categories")
async def api_sync_categories(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    settings = get_ebay_settings(business_id)
    try:
        token = get_ebay_access_token(business_id)
    except Exception as e:
        raise HTTPException(400, str(e))
    try:
        count = sync_ebay_categories(token)
        return {"ok": True, "count": count}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/listings/{item_id}/auto-category")
async def api_auto_category(item_id: str, request: Request, broad: bool = False, exclude: str = None, query: str = None):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        res = supabase.table("listings").select("title").eq("id", item_id).limit(1).execute()
        if not res.data:
            raise HTTPException(404, "Listing not found")
        title = query if query else res.data[0].get("title", "")
        suggestion = suggest_ebay_category(title, business_id, restrict=not broad, exclude_id=exclude)
        if not suggestion:
            raise HTTPException(404, "No matching category found — try 'Search all categories' or check your eBay token")
        supabase.table("listings").update({"ebay_category_id": suggestion["category_id"]}).eq("id", item_id).execute()
        return {"ok": True, **suggestion}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/ebay/categories-tree")
async def categories_tree(request: Request, root: str = None):
    """Build a nested tree from the locally synced ebay_categories table, so the
    Categories page can render a collapsible tree instead of a flat list."""
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        # Detect whether is_leaf column exists yet (older syncs won't have it) — probe once up front
        use_is_leaf = True
        try:
            supabase.table("ebay_categories").select("is_leaf").limit(1).execute()
        except Exception:
            use_is_leaf = False

        select_fields = "category_id,name,path,is_leaf" if use_is_leaf else "category_id,name,path"
        all_rows = []
        offset = 0
        page_size = 1000
        while True:
            q = supabase.table("ebay_categories").select(select_fields)
            if root:
                q = q.ilike("path", f"{root}%")
            res = q.range(offset, offset + page_size - 1).execute()
            batch = res.data or []
            all_rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        tree = {}
        for row in all_rows:
            parts = [p.strip() for p in (row.get("path") or "").split(">") if p.strip()]
            if not parts:
                continue
            node = tree
            for i, part in enumerate(parts):
                if part not in node:
                    node[part] = {"__children__": {}, "__id__": None, "__leaf__": False}
                if i == len(parts) - 1:
                    node[part]["__id__"] = row["category_id"]
                    # Old schema (no is_leaf column) only ever stored leaf categories, so default True there
                    node[part]["__leaf__"] = bool(row.get("is_leaf")) if use_is_leaf else True
                node = node[part]["__children__"]
        return {"tree": tree}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/categories", response_class=HTMLResponse)
async def categories_page(request: Request):
    business_id, is_admin = get_business_info(request)
    if not business_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("categories.html", {"request": request, "is_admin": is_admin})

@app.get("/api/ebay/category-search")
async def ebay_category_search(q: str, request: Request):
    """Look up valid LEAF category IDs by keyword, using the token already saved in Settings."""
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import requests as _req
    settings = get_ebay_settings(business_id)
    try:
        token = get_ebay_access_token(business_id)
    except Exception as e:
        raise HTTPException(400, str(e))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    r = _req.get(
        "https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/get_category_suggestions",
        headers=headers, params={"q": q}, timeout=15
    )
    if r.status_code != 200:
        raise HTTPException(500, f"{r.status_code}: {r.text}")
    data = r.json()
    out = []
    for s in data.get("categorySuggestions", []):
        cat = s.get("category", {})
        path = " > ".join(a.get("categoryName","") for a in s.get("categoryTreeNodeAncestors", [])[::-1])
        out.append({"id": cat.get("categoryId"), "name": cat.get("categoryName"), "path": path})
    return {"results": out}

@app.get("/api/ebay/policies")
async def list_ebay_policies(request: Request):
    """Fetch payment/return/fulfillment policy IDs using the token already saved in Settings."""
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import requests as _req
    settings = get_ebay_settings(business_id)
    try:
        token = get_ebay_access_token(business_id)
    except Exception as e:
        raise HTTPException(400, str(e))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    base = "https://api.ebay.com/sell/account/v1"
    out = {}
    mapping = {
        "payment_policy": ("paymentPolicies", "paymentPolicyId"),
        "return_policy": ("returnPolicies", "returnPolicyId"),
        "fulfillment_policy": ("fulfillmentPolicies", "fulfillmentPolicyId"),
    }
    for kind, (list_key, id_key) in mapping.items():
        r = _req.get(f"{base}/{kind}?marketplace_id=EBAY_US", headers=headers, timeout=15)
        if r.status_code != 200:
            out[kind] = {"error": f"{r.status_code}: {r.text}"}
            continue
        data = r.json()
        out[kind] = [{"name": p.get("name"), "id": p.get(id_key)} for p in data.get(list_key, [])]
    return out

@app.post("/api/listings/{item_id}/rescan")
async def rescan_listing(item_id: str, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        res = supabase.table("listings").select("photo_id").eq("id", item_id).limit(1).execute()
        pid = (res.data[0].get("photo_id", "") if res.data else "")
        if pid:
            grp = supabase.table("group_photos").select("group_id").eq("photo_id", pid).limit(1).execute()
            if grp.data:
                gid = grp.data[0]["group_id"]
                supabase.table("listing_groups").update({"status": "pending"}).eq("id", gid).execute()
        supabase.table("listings").update({"status": "pending", "title": "Scanning..."}).eq("id", item_id).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

class RestoreItems(BaseModel):
    ids: Optional[list] = None  # None/omitted = restore ALL archived items

@app.post("/api/listings/restore")
async def restore_listings(request: Request, body: RestoreItems):
    business_id = require_auth(request)
    if not business_id:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        q = supabase.table("listings").update({"status": "pending"}).eq("business_id", business_id).eq("status", "archived")
        if body.ids:
            q = q.in_("id", [str(i) for i in body.ids])
        res = q.execute()
        return {"ok": True, "count": len(res.data or [])}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/financials", response_class=HTMLResponse)
async def financials_page(request: Request):
    business_id, is_admin = get_business_info(request)
    if not business_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("financials.html", {"request": request, "is_admin": is_admin})

@app.get("/acquisitions", response_class=HTMLResponse)
async def acquisitions_page(request: Request):
    business_id, is_admin = get_business_info(request)
    if not business_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("acquisitions.html", {"request": request, "is_admin": is_admin})

class AcquisitionCreate(BaseModel):
    sku: str
    name: Optional[str] = None
    payment_method: Optional[str] = None
    date: Optional[str] = None
    cost: Optional[float] = None
    cash: Optional[float] = None
    notes: Optional[str] = None

@app.get("/api/acquisitions")
async def list_acquisitions(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    res = supabase.table("acquisitions").select("*").eq("business_id", business_id)\
        .order("date", desc=True).execute()
    acquisitions = res.data or []

    # eBay is the sum of stored final_net (already includes shipping — see apply_shipping_matches)
    # from every synced order whose SKU starts with "<sku>-" (same lot-prefix convention
    # Financials uses).
    skus = list(set(a["sku"] for a in acquisitions if a.get("sku")))
    ebay_by_lot = {}
    if skus:
        orders_res = supabase.table("orders").select("sku,final_net").eq("business_id", business_id).execute()
        for row in (orders_res.data or []):
            order_sku = row.get("sku") or ""
            if "-" not in order_sku:
                continue
            prefix = order_sku.split("-", 1)[0]
            if prefix in skus:
                ebay_by_lot[prefix] = ebay_by_lot.get(prefix, 0.0) + (row.get("final_net") or 0)

    for a in acquisitions:
        ebay = round(ebay_by_lot.get(a["sku"], 0.0), 2)
        cash = a.get("cash") or 0
        cost = a.get("cost") or 0
        total_payouts = round(cash + ebay, 2)
        profit = round(total_payouts - cost, 2)
        a["ebay"] = ebay
        a["total_payouts"] = total_payouts
        a["profit"] = profit
        a["roi_pct"] = round(profit / cost * 100, 1) if cost else None

    return {"acquisitions": acquisitions}

@app.post("/api/acquisitions")
async def create_acquisition(request: Request, body: AcquisitionCreate):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        record = body.dict()
        record["business_id"] = business_id
        res = supabase.table("acquisitions").insert(record).execute()
        return {"ok": True, "acquisition": (res.data or [{}])[0]}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.delete("/api/acquisitions/{acquisition_id}")
async def delete_acquisition(acquisition_id: str, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        supabase.table("acquisitions").delete().eq("id", acquisition_id).eq("business_id", business_id).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

def _parse_money(val) -> Optional[float]:
    """Handles '$1,550 ', '$0 ', '', None -> float or None"""
    if val is None:
        return None
    s = str(val).strip().replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None

def _parse_acq_date(val) -> Optional[str]:
    """Handles the mixed formats seen in the existing spreadsheet:
    'May-23' (Mon-YY), '4/10/2024' (M/D/YYYY), '1/1/9999' (placeholder/unknown -> None)."""
    import datetime as _dt
    s = str(val).strip()
    if not s or s.startswith("1/1/9999"):
        return None
    for fmt in ("%b-%y", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

@app.post("/api/acquisitions/upload-csv")
async def upload_acquisitions_csv(request: Request, file: UploadFile = File(...)):
    """Only imports the true manual-input columns (SKU, Name, Payment_Method, Date, Cost,
    Cash) — eBay, Total_Payouts, and Profit from the old spreadsheet are NOT imported,
    since those are now computed live instead of stored as a static snapshot."""
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import io, csv as _csv

    content = await file.read()
    text = content.decode("utf-8-sig", errors="replace")
    reader = _csv.DictReader(io.StringIO(text))

    inserted = 0
    skipped = 0
    for row in reader:
        sku = str(row.get("SKU") or "").strip()
        if not sku:
            skipped += 1
            continue
        record = {
            "business_id": business_id,
            "sku": sku,
            "name": str(row.get("Name") or "").strip() or None,
            "payment_method": str(row.get("Payment_Method") or "").strip() or None,
            "date": _parse_acq_date(row.get("Date")),
            "cost": _parse_money(row.get("Cost")),
            "cash": _parse_money(row.get("Cash")),
        }
        try:
            supabase.table("acquisitions").insert(record).execute()
            inserted += 1
        except Exception:
            skipped += 1

    return {"ok": True, "inserted": inserted, "skipped": skipped}

def fetch_ebay_orders(business_id: str, start_iso: str, end_iso: str) -> list:
    """Returns normalized order-line rows from eBay's Fulfillment API for the given date range."""
    import requests as _req
    token = get_ebay_access_token(business_id)
    rows = []
    offset = 0
    max_pages = 25  # 25 * 200 = 5,000 orders safety cap
    for _ in range(max_pages):
        r = _req.get(
            f"{EBAY_API_BASE}/sell/fulfillment/v1/order",
            headers=ebay_headers(token, content_language=False),
            params={
                "filter": f"creationdate:[{start_iso}..{end_iso}]",
                "limit": 200,
                "offset": offset,
            },
            timeout=20,
        )
        if r.status_code != 200:
            raise Exception(f"eBay getOrders failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        for order in data.get("orders", []):
            created = order.get("creationDate", "")
            for li in order.get("lineItems", []):
                rows.append({
                    "platform": "eBay",
                    "sku": li.get("sku") or "(no SKU)",
                    "title": li.get("title", ""),
                    "quantity": int(li.get("quantity", 1)),
                    "revenue": float((li.get("lineItemCost") or {}).get("value", 0) or 0),
                    "order_date": created[:10] if created else "",
                    "order_id": order.get("orderId", ""),
                    "line_item_id": li.get("lineItemId", ""),
                })
        total = data.get("total", 0)
        offset += 200
        if offset >= total or not data.get("orders"):
            break
    return rows

def fetch_ebay_fees_by_line_item(business_id: str, start_iso: str, end_iso: str) -> dict:
    """Returns {(order_id, line_item_id): net_fee_amount} for marketplace fees, AND
    {order_id: label_cost} for eBay-purchased shipping labels (SHIPPING_LABEL transaction
    type) — both captured from the SAME Finance API pass, no extra API calls needed.
    Note: eBay only associates a SHIPPING_LABEL transaction with a specific orderId when
    the label was purchased individually — bulk/batch label purchases only report one
    lump amount with no per-order breakdown, so those can't be attributed here.
    eBay hard-caps each query's date span at 36 months and total retention at 5 years,
    so a wide range (e.g. a full historical backfill) gets auto-split into ~30-month
    chunks here rather than failing outright."""
    import datetime as _dt

    def _fetch_window(window_start_iso: str, window_end_iso: str) -> tuple:
        import requests as _req
        token = get_ebay_access_token(business_id)
        window_fees = {}
        window_labels = {}
        offset = 0
        max_pages = 25  # 25 * 200 = 5,000 transactions safety cap per window
        for _ in range(max_pages):
            r = _req.get(
                "https://apiz.ebay.com/sell/finances/v1/transaction",
                headers=ebay_headers(token, content_language=False),
                params={
                    "filter": f"transactionDate:[{window_start_iso}..{window_end_iso}]",
                    "limit": 200,
                    "offset": offset,
                },
                timeout=20,
            )
            if r.status_code != 200:
                raise Exception(f"eBay Finance transactions failed ({r.status_code}): {r.text[:300]}")
            data = r.json()
            for txn in data.get("transactions", []):
                order_id = txn.get("orderId", "")
                if txn.get("transactionType") == "SHIPPING_LABEL" and order_id:
                    amt = float((txn.get("amount") or {}).get("value", 0) or 0)
                    window_labels[order_id] = window_labels.get(order_id, 0.0) + amt
                    continue
                for li in txn.get("orderLineItems", []) or []:
                    line_item_id = li.get("lineItemId", "")
                    if not order_id or not line_item_id:
                        continue
                    key = (order_id, line_item_id)
                    fee_total = 0.0
                    for fee in li.get("marketplaceFees", []) or []:
                        fee_total += float((fee.get("amount") or {}).get("value", 0) or 0)
                    window_fees[key] = window_fees.get(key, 0.0) + fee_total
            total = data.get("total", 0)
            offset += 200
            if offset >= total or not data.get("transactions"):
                break
        return window_fees, window_labels

    start_dt = _dt.datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%S.%fZ")
    end_dt = _dt.datetime.strptime(end_iso, "%Y-%m-%dT%H:%M:%S.%fZ")
    fees = {}
    ebay_labels_by_order = {}
    window_start = start_dt
    while window_start < end_dt:
        window_end = min(window_start + _dt.timedelta(days=900), end_dt)  # ~30 months per chunk
        window_fees, window_labels = _fetch_window(
            window_start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            window_end.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        )
        fees.update(window_fees)
        ebay_labels_by_order.update(window_labels)
        window_start = window_end
    return {"fees_by_line": fees, "ebay_labels_by_order": ebay_labels_by_order}

def fetch_shopify_orders(business_id: str, start_iso: str, end_iso: str) -> list:
    """Returns normalized order-line rows from Shopify's Admin API for the given date range,
    with refunds subtracted (from the embedded order payload) and an ESTIMATED payment-processing
    fee (Shopify's standard 2.9%+$0.30 rate, applied only when payment_gateway_names shows
    Shopify Payments/Shop Pay was used) prorated across line items. This is an estimate, not
    the exact fee — getting the real figure requires one extra API call per order, which made
    this page unusably slow on any real order volume."""
    import requests as _req
    settings = get_ebay_settings(business_id)
    domain = (settings.get("SHOPIFY_STORE_DOMAIN", "") or "").strip().replace("https://", "").replace("http://", "").strip("/")
    if not domain:
        return []
    token = get_shopify_access_token(business_id)
    rows = []
    url = f"https://{domain}/admin/api/2024-10/orders.json"
    params = {"status": "any", "created_at_min": start_iso, "created_at_max": end_iso, "limit": 250}
    headers = {"X-Shopify-Access-Token": token}
    while url:
        r = _req.get(url, headers=headers, params=params, timeout=20)
        if r.status_code == 401:
            token = get_shopify_access_token(business_id, force_refresh=True)
            headers["X-Shopify-Access-Token"] = token
            r = _req.get(url, headers=headers, params=params, timeout=20)
        if r.status_code != 200:
            raise Exception(f"Shopify orders fetch failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        for order in data.get("orders", []):
            created = order.get("created_at", "")
            order_id = order.get("id")

            # Refunds are embedded in the order payload already — no extra call needed.
            refunded_by_line = {}
            for refund in order.get("refunds", []) or []:
                for rli in refund.get("refund_line_items", []) or []:
                    lid = rli.get("line_item_id")
                    amt = float(rli.get("subtotal", 0) or 0) + float(rli.get("total_tax", 0) or 0)
                    refunded_by_line[lid] = refunded_by_line.get(lid, 0.0) + amt

            # Payment-processing fee: getting the EXACT fee requires one extra API call per
            # order (the transactions sub-resource), which was making this page painfully slow
            # on any real order volume. Instead, estimate using Shopify's standard online rate
            # (2.9% + $0.30) when the order actually went through Shopify Payments — no extra
            # call needed, since payment_gateway_names is already in the order payload.
            gateways = order.get("payment_gateway_names", []) or []
            used_shopify_payments = any("shopify_payments" in g.lower() or "shop_pay" in g.lower() for g in gateways)
            order_total = float(order.get("total_price", 0) or 0)
            order_fee_total = (order_total * 0.029 + 0.30) if (used_shopify_payments and order_total > 0) else 0.0

            line_items = order.get("line_items", [])
            order_subtotal = sum(float(li.get("price", 0) or 0) * int(li.get("quantity", 1)) for li in line_items)

            for li in line_items:
                gross = float(li.get("price", 0) or 0) * int(li.get("quantity", 1))
                refund_amt = refunded_by_line.get(li.get("id"), 0.0)
                fee_share = (order_fee_total * (gross / order_subtotal)) if order_subtotal > 0 else 0.0
                rows.append({
                    "platform": "Shopify",
                    "sku": li.get("sku") or "(no SKU)",
                    "title": li.get("title", ""),
                    "quantity": int(li.get("quantity", 1)),
                    "revenue": gross,
                    "refund": refund_amt,
                    "fee": fee_share,
                    "net": gross - refund_amt - fee_share,
                    "order_date": created[:10] if created else "",
                    "order_id": order.get("name", ""),
                })
        # Shopify cursor pagination via Link header
        link = r.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        url = next_url
        params = None  # next_url already has query params baked in
    return rows

def apply_shipping_matches(business_id: str) -> dict:
    """Matches every order's tracking_number against shipping_labels and writes the
    result directly into orders.shipping_cost / orders.final_net — a stored, one-time
    computation, not something recalculated on every page read. Pure local DB work,
    no eBay/Shopify API calls, batched so it's fast even across thousands of orders."""
    orders_res = supabase.table("orders").select("id,net,tracking_number").eq("business_id", business_id)\
        .not_.is_("tracking_number", "null").execute()
    orders = orders_res.data or []
    if not orders:
        return {"updated": 0}

    labels_res = supabase.table("shipping_labels").select("tracking_number,cost").eq("business_id", business_id).execute()
    cost_by_tracking = {row["tracking_number"]: (row.get("cost") or 0) for row in (labels_res.data or [])}

    updates = []
    for order in orders:
        trackings = (order.get("tracking_number") or "").split(",")
        shipping_cost = round(sum(cost_by_tracking.get(tn, 0) or 0 for tn in trackings if tn), 2)
        if shipping_cost <= 0:
            continue  # nothing to update — leave existing stored values alone
        base_net = order.get("net") or 0
        updates.append({"id": order["id"], "shipping_cost": shipping_cost, "final_net": round(base_net - shipping_cost, 2)})

    updated = 0
    for i in range(0, len(updates), 500):
        chunk = updates[i:i+500]
        try:
            supabase.table("orders").upsert(chunk).execute()
            updated += len(chunk)
        except Exception as e:
            print(f"apply_shipping_matches: batch {i}-{i+len(chunk)} failed: {e}")

    return {"updated": updated, "orders_with_tracking": len(orders)}

@app.post("/api/financials/apply-shipping-matches")
async def apply_shipping_matches_now(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        result = apply_shipping_matches(business_id)
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/shipping-labels/upload")
async def upload_shipping_labels(request: Request, file: UploadFile = File(...)):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import io, csv as _csv

    content = await file.read()
    rows = []
    filename = (file.filename or "").lower()

    if filename.endswith(".csv"):
        text = content.decode("utf-8-sig", errors="replace")
        reader = _csv.DictReader(io.StringIO(text))
        rows = list(reader)
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        ws = wb.active
        raw_rows = list(ws.iter_rows(values_only=True))
        if not raw_rows:
            raise HTTPException(400, "File appears empty")
        headers = [str(h).strip() if h else "" for h in raw_rows[0]]
        for r in raw_rows[1:]:
            if not any(r):
                continue
            rows.append({headers[i]: r[i] for i in range(len(headers)) if i < len(r)})

    records = []
    skipped = 0
    for row in rows:
        tracking = str(row.get("Tracking Number") or "").strip()
        if not tracking:
            skipped += 1
            continue
        cost = row.get("Cost")
        try:
            cost = float(cost) if cost not in (None, "") else None
        except (ValueError, TypeError):
            cost = None
        records.append({
            "tracking_number": tracking,
            "business_id": business_id,
            "recipient": str(row.get("Recipient") or ""),
            "cost": cost,
            "created_date": str(row.get("Created Date") or ""),
            "ship_from": str(row.get("Ship From") or ""),
            "source": str(row.get("Source") or ""),
        })

    inserted = 0
    for i in range(0, len(records), 500):
        chunk = records[i:i+500]
        try:
            supabase.table("shipping_labels").upsert(chunk).execute()
            inserted += len(chunk)
        except Exception as e:
            print(f"upload_shipping_labels: batch {i}-{i+len(chunk)} failed: {e}")
            skipped += len(chunk)

    match_result = apply_shipping_matches(business_id)

    return {"ok": True, "inserted": inserted, "skipped": skipped, "total_rows": len(rows), "matched": match_result.get("updated", 0)}

@app.post("/api/financials/match-shipping")
async def match_shipping_costs(request: Request, body: dict = Body(...)):
    """Given a list of eBay order IDs (from the currently loaded Financials view),
    fetches each order's tracking number from eBay and matches it against uploaded
    Pirate Ship data by tracking number. Deliberately opt-in (not automatic) since
    it's one extra API call per order."""
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import requests as _req
    order_ids = list(set(body.get("order_ids", [])))[:300]  # sane cap per click
    token = get_ebay_access_token(business_id)

    tracking_by_order = {}
    for oid in order_ids:
        try:
            r = _req.get(
                f"{EBAY_API_BASE}/sell/fulfillment/v1/order/{oid}/shipping_fulfillment",
                headers=ebay_headers(token, content_language=False),
                timeout=15,
            )
            if r.status_code == 200:
                fulfillments = r.json().get("fulfillments", [])
                for f in fulfillments:
                    tn = f.get("shipmentTrackingNumber")
                    if tn:
                        tracking_by_order.setdefault(oid, []).append(tn)
        except Exception:
            continue

    all_trackings = [tn for lst in tracking_by_order.values() for tn in lst]
    cost_by_tracking = {}
    if all_trackings:
        res = supabase.table("shipping_labels").select("tracking_number,cost")\
            .eq("business_id", business_id).in_("tracking_number", all_trackings).execute()
        for row in (res.data or []):
            cost_by_tracking[row["tracking_number"]] = row.get("cost")

    result = {}
    for oid, trackings in tracking_by_order.items():
        cost = sum(cost_by_tracking.get(tn, 0) or 0 for tn in trackings)
        result[oid] = {"tracking_numbers": trackings, "shipping_cost": round(cost, 2)}

    return {"by_order": result, "matched": len(result), "requested": len(order_ids)}

def _sync_orders_window(business_id: str, start_iso: str, end_iso: str) -> dict:
    """Does the actual API pulling + upserting for ONE date window. Called repeatedly,
    once per month, by sync_orders_for_business below — never call this directly with
    a huge range, since each call blocks for as long as that window takes."""
    import requests as _req, datetime as _dt

    upserted = 0
    errors = {}

    # --- eBay: orders + real fees + tracking numbers ---
    try:
        ebay_rows = fetch_ebay_orders(business_id, start_iso, end_iso)
        try:
            fee_data = fetch_ebay_fees_by_line_item(business_id, start_iso, end_iso)
            fees_by_line = fee_data["fees_by_line"]
            ebay_labels_by_order = fee_data["ebay_labels_by_order"]
        except Exception as e:
            fees_by_line = {}
            ebay_labels_by_order = {}
            errors["ebay_fees"] = str(e)

        # Tracking numbers, one call per unique order (needed for shipping cost matching)
        token = get_ebay_access_token(business_id)
        order_ids = list(set(r["order_id"] for r in ebay_rows if r.get("order_id")))
        tracking_by_order = {}
        for oid in order_ids[:500]:  # safety cap per sync cycle
            try:
                r = _req.get(
                    f"{EBAY_API_BASE}/sell/fulfillment/v1/order/{oid}/shipping_fulfillment",
                    headers=ebay_headers(token, content_language=False), timeout=15,
                )
                if r.status_code == 200:
                    for f in r.json().get("fulfillments", []):
                        tn = f.get("shipmentTrackingNumber")
                        if tn:
                            tracking_by_order.setdefault(oid, []).append(tn)
            except Exception:
                continue

        all_trackings = [tn for lst in tracking_by_order.values() for tn in lst]
        cost_by_tracking = {}
        # Chunk the .in_() lookup — a huge tracking-number list in one query can produce
        # an oversized request that Supabase rejects outright.
        for i in range(0, len(all_trackings), 200):
            chunk = all_trackings[i:i+200]
            try:
                res = supabase.table("shipping_labels").select("tracking_number,cost")\
                    .eq("business_id", business_id).in_("tracking_number", chunk).execute()
                for row in (res.data or []):
                    cost_by_tracking[row["tracking_number"]] = row.get("cost") or 0
            except Exception as e:
                errors["shipping_match"] = str(e)

        def _safe(n):
            """NaN/Infinity are valid Python floats but not valid JSON — a single bad
            fee value here would otherwise silently corrupt the whole upsert payload."""
            try:
                n = float(n)
                return round(n, 2) if (n == n and abs(n) != float("inf")) else 0.0  # n==n is False only for NaN
            except (TypeError, ValueError):
                return 0.0

        skipped_rows = 0
        for row in ebay_rows:
            fee = _safe(fees_by_line.get((row["order_id"], row["line_item_id"]), 0.0))
            revenue = _safe(row["revenue"])
            net = _safe(revenue - fee)
            trackings = tracking_by_order.get(row["order_id"], [])
            pirate_ship_cost = sum(cost_by_tracking.get(tn, 0) or 0 for tn in trackings)
            # Pirate Ship match takes priority (it's the common case); eBay-purchased
            # label cost fills in only when there's no Pirate Ship match for this order.
            shipping_cost = _safe(pirate_ship_cost if pirate_ship_cost > 0 else ebay_labels_by_order.get(row["order_id"], 0))
            record = {
                "id": f"ebay:{row['order_id']}:{row['line_item_id']}",
                "business_id": business_id, "platform": "eBay", "order_id": row["order_id"],
                "sku": row["sku"], "title": row["title"], "quantity": row["quantity"],
                "order_date": row["order_date"], "gross_revenue": revenue,
                "fee": fee, "net": net,
                "tracking_number": ",".join(trackings) if trackings else None,
                "shipping_cost": shipping_cost,
                "final_net": _safe(net - shipping_cost),
            }
            try:
                supabase.table("orders").upsert(record).execute()
                upserted += 1
            except Exception as e:
                skipped_rows += 1
                print(f"sync_orders_for_business: skipped bad row {record.get('id')}: {e}")
        if skipped_rows:
            errors["ebay_skipped_rows"] = f"{skipped_rows} row(s) failed to upsert — see logs for details"
    except Exception as e:
        errors["ebay"] = str(e)

    # --- Shopify: orders + estimated fee + embedded refunds ---
    try:
        def _safe2(n):
            try:
                n = float(n)
                return round(n, 2) if (n == n and abs(n) != float("inf")) else 0.0
            except (TypeError, ValueError):
                return 0.0

        shopify_rows = fetch_shopify_orders(business_id, start_iso, end_iso)
        shopify_skipped = 0
        for i, row in enumerate(shopify_rows):
            net = _safe2(row.get("net", row["revenue"]))
            record = {
                "id": f"shopify:{row['order_id']}:{row['sku']}:{i}",
                "business_id": business_id, "platform": "Shopify", "order_id": row["order_id"],
                "sku": row["sku"], "title": row["title"], "quantity": row["quantity"],
                "order_date": row["order_date"], "gross_revenue": _safe2(row["revenue"]),
                "fee": _safe2(row.get("fee", 0)), "net": net,
                "tracking_number": None, "shipping_cost": 0,
                "final_net": net,
            }
            try:
                supabase.table("orders").upsert(record).execute()
                upserted += 1
            except Exception as e:
                shopify_skipped += 1
                print(f"sync_orders_for_business: skipped bad Shopify row {record.get('id')}: {e}")
        if shopify_skipped:
            errors["shopify_skipped_rows"] = f"{shopify_skipped} row(s) failed to upsert — see logs for details"
    except Exception as e:
        errors["shopify"] = str(e)

    return {"upserted": upserted, "errors": errors}

def sync_orders_for_business(business_id: str, days_back: int = 90) -> dict:
    """Pulls orders (with real fees, refunds, and shipping cost) from eBay + Shopify and
    upserts them into the local `orders` table — one MONTH at a time, not the whole
    range in one shot. Each month is its own independent call: if one month fails or
    times out, the others still land, and the next run just continues from wherever
    it left off (upserts are idempotent, so re-running already-synced months is harmless).
    This is the ONLY place that hits the live APIs for financial data — Financials
    itself just queries the local table, so filtering by date range is instant."""
    import datetime as _dt

    now = _dt.datetime.utcnow()
    range_start = now - _dt.timedelta(days=days_back)
    total_upserted = 0
    all_errors = {}

    month_start = range_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month_start < now:
        next_month = (month_start.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        window_end = min(next_month, now) - _dt.timedelta(seconds=1)
        window_start_iso = max(month_start, range_start).strftime("%Y-%m-%dT00:00:00.000Z")
        window_end_iso = window_end.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        label = month_start.strftime("%Y-%m")
        try:
            result = _sync_orders_window(business_id, window_start_iso, window_end_iso)
            total_upserted += result["upserted"]
            if result.get("errors"):
                all_errors[label] = result["errors"]
            print(f"sync_orders_for_business: month {label} -> {result['upserted']} row(s)"
                  + (f", errors: {result['errors']}" if result.get("errors") else ""))
        except Exception as e:
            all_errors[label] = str(e)
            print(f"sync_orders_for_business: month {label} FAILED entirely: {e}")

        month_start = next_month

    return {"upserted": total_upserted, "errors": all_errors}


_sync_status = {}  # business_id -> {"running": bool, "result": dict|None, "started_at": iso, "finished_at": iso|None}

async def _run_sync_background(business_id: str, days_back: int):
    import asyncio, datetime as _dt
    try:
        result = await asyncio.to_thread(sync_orders_for_business, business_id, days_back)
        _sync_status[business_id] = {
            "running": False, "result": result,
            "started_at": _sync_status.get(business_id, {}).get("started_at"),
            "finished_at": _dt.datetime.utcnow().isoformat(),
        }
    except Exception as e:
        _sync_status[business_id] = {
            "running": False, "result": {"upserted": 0, "errors": {"fatal": str(e)}},
            "started_at": _sync_status.get(business_id, {}).get("started_at"),
            "finished_at": _dt.datetime.utcnow().isoformat(),
        }

def backfill_tracking_numbers(business_id: str) -> dict:
    """Fills in tracking_number for orders that don't have one, WITHOUT re-fetching
    orders or fee data at all — just the per-order shipping_fulfillment lookup, which
    is the only piece that was ever wrong. Much faster than a full sync_orders_for_business
    pass since it skips getOrders and the Finance API entirely."""
    import requests as _req

    res = supabase.table("orders").select("id,order_id").eq("business_id", business_id)\
        .eq("platform", "eBay").is_("tracking_number", "null").execute()
    rows = res.data or []
    order_ids = list(set(r["order_id"] for r in rows if r.get("order_id")))

    token = get_ebay_access_token(business_id)
    tracking_by_order = {}
    for oid in order_ids:
        try:
            r = _req.get(
                f"{EBAY_API_BASE}/sell/fulfillment/v1/order/{oid}/shipping_fulfillment",
                headers=ebay_headers(token, content_language=False), timeout=15,
            )
            if r.status_code == 200:
                for f in r.json().get("fulfillments", []):
                    tn = f.get("shipmentTrackingNumber")
                    if tn:
                        tracking_by_order.setdefault(oid, []).append(tn)
        except Exception:
            continue

    updated = 0
    for row in rows:
        trackings = tracking_by_order.get(row["order_id"])
        if trackings:
            try:
                supabase.table("orders").update({"tracking_number": ",".join(trackings)}).eq("id", row["id"]).execute()
                updated += 1
            except Exception:
                pass

    return {"updated": updated, "orders_checked": len(order_ids), "rows_missing": len(rows)}

@app.post("/api/financials/backfill-tracking")
async def backfill_tracking_now(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import asyncio
    if _sync_status.get(business_id, {}).get("running"):
        return {"ok": True, "already_running": True}
    _sync_status[business_id] = {"running": True, "result": None, "started_at": __import__("datetime").datetime.utcnow().isoformat(), "finished_at": None}
    async def _run():
        import datetime as _dt
        try:
            result = await asyncio.to_thread(backfill_tracking_numbers, business_id)
            _sync_status[business_id] = {"running": False, "result": result, "started_at": _sync_status.get(business_id, {}).get("started_at"), "finished_at": _dt.datetime.utcnow().isoformat()}
        except Exception as e:
            _sync_status[business_id] = {"running": False, "result": {"error": str(e)}, "started_at": _sync_status.get(business_id, {}).get("started_at"), "finished_at": _dt.datetime.utcnow().isoformat()}
    asyncio.create_task(_run())
    return {"ok": True, "started": True}

@app.post("/api/financials/sync-now")
async def sync_now(request: Request, days_back: int = 90):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import asyncio, datetime as _dt
    if _sync_status.get(business_id, {}).get("running"):
        return {"ok": True, "already_running": True}
    _sync_status[business_id] = {"running": True, "result": None, "started_at": _dt.datetime.utcnow().isoformat(), "finished_at": None}
    asyncio.create_task(_run_sync_background(business_id, days_back))
    return {"ok": True, "started": True}

@app.get("/api/financials/sync-status")
async def sync_status(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    return _sync_status.get(business_id, {"running": False, "result": None, "started_at": None, "finished_at": None})

@app.get("/api/financials")
async def api_financials(request: Request, start: str = None, end: str = None, include_shopify: bool = True):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import datetime as _dt
    now = _dt.datetime.utcnow()
    end_dt = _dt.datetime.fromisoformat(end) if end else now
    start_dt = _dt.datetime.fromisoformat(start) if start else (end_dt - _dt.timedelta(days=30))
    start_date_str = start_dt.strftime("%Y-%m-%d")
    end_date_str = end_dt.strftime("%Y-%m-%d")

    # Financials reads straight from the local `orders` table — shipping_cost and final_net
    # are already-computed, stored columns (written by apply_shipping_matches whenever a
    # Pirate Ship CSV is uploaded, and by the sync jobs). No computation happens here.
    query = supabase.table("orders").select("*").eq("business_id", business_id)\
        .gte("order_date", start_date_str).lte("order_date", end_date_str)
    if not include_shopify:
        query = query.eq("platform", "eBay")
    res = query.execute()
    rows = res.data or []
    rows.sort(key=lambda r: r.get("order_date", ""), reverse=True)

    order_lines = [{
        "sku": r["sku"], "title": r["title"], "platform": r["platform"], "order_id": r["order_id"],
        "quantity": r["quantity"], "revenue": r["gross_revenue"], "net": r["final_net"],
        "shipping_cost": r.get("shipping_cost") or 0, "order_date": r.get("order_date", ""),
    } for r in rows]

    last_sync_res = supabase.table("orders").select("synced_at").eq("business_id", business_id)\
        .order("synced_at", desc=True).limit(1).execute()
    last_synced_at = (last_sync_res.data or [{}])[0].get("synced_at")

    return {
        "start": start_date_str,
        "end": end_date_str,
        "order_lines": order_lines,
        "last_synced_at": last_synced_at,
        "totals": {
            "orders": len(rows),
            "quantity": sum(r["quantity"] for r in rows),
            "revenue": round(sum(r["gross_revenue"] for r in rows), 2),
            "net": round(sum(r["final_net"] for r in rows), 2),
        },
        "errors": {},
    }

@app.get("/archive", response_class=HTMLResponse)
async def archive_page(request: Request):
    business_id, is_admin = get_business_info(request)
    if not business_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("archive.html", {"request": request, "is_admin": is_admin})

@app.post("/api/listings/archive-batch")
async def archive_batch(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        res = supabase.table("listings").select("*").eq("business_id", business_id).neq("status", "archived").execute()
        items = res.data or []
        if items:
            ids = [str(i["id"]) for i in items]
            supabase.table("listings").update({"status": "archived"}).in_("id", ids).execute()
        return {"ok": True, "count": len(items)}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── API: BATCH UPLOAD ─────────────────────────────────────────── #

class CreateGroup(BaseModel):
    session_id: str
    condition:  str

@app.post("/api/groups")
async def create_group(body: CreateGroup, request: Request):
    try:
        business_id = require_auth(request)
        # Check scan limit
        biz = supabase.table("businesses").select("scan_count,scan_limit").eq("id", business_id).execute()
        if biz.data:
            scan_count = biz.data[0].get("scan_count") or 0
            scan_limit = biz.data[0].get("scan_limit") or 50
            if scan_count >= scan_limit:
                raise HTTPException(402, "Scan limit reached. Please contact us to upgrade your plan.")
            # Increment scan count
            supabase.table("businesses").update({"scan_count": scan_count + 1}).eq("id", business_id).execute()
        res = supabase.table("listing_groups").insert({
            "session_id": body.session_id,
            "status":     "waiting",
            "quantity":   1,
            "condition":  body.condition,
            "business_id": business_id,
        }).execute()
        import traceback
        print(f"Group insert result: {res}")
        data = res.data
        gid = data[0]["id"] if isinstance(data, list) and data else (data.get("id") if isinstance(data, dict) else None)
        if not gid:
            raise Exception(f"No ID returned. data={data}")
        return {"id": gid}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(500, str(e))

class SubmitGroup(BaseModel):
    group_id:  str
    condition: str
    quantity:  int

@app.post("/api/groups/{group_id}/rescan")
async def rescan_group(group_id: str, request: Request):
    business_id = require_auth(request)
    try:
        supabase.table("listing_groups").update({"status": "pending"}).eq("id", group_id).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/groups/{group_id}/listing")
async def get_group_listing(group_id: str, request: Request):
    try:
        gp = supabase.table("group_photos").select("photo_id").eq("group_id", group_id).limit(1).execute()
        if not gp.data:
            return {"listing": None}
        photo_id = gp.data[0]["photo_id"]
        res = supabase.table("listings").select("id,status,title,price").eq("photo_id", photo_id).limit(1).execute()
        return {"listing": res.data[0] if res.data else None}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/groups/submit")
async def submit_group(body: SubmitGroup, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        supabase.table("listing_groups").update({
            "condition": body.condition,
            "quantity":  body.quantity,
            "status":    "pending",
        }).eq("id", body.group_id).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/photos/upload")
async def upload_photo(request: Request):
    try:
        form     = await request.form()
        file     = form["file"]
        gid      = str(form["group_id"])
        idx      = int(form.get("index", 0))
        contents = await file.read()
        dt  = datetime.now()
        fn  = f"{dt.strftime('%d%m%y')}_{dt.strftime('%H%M%S')}_{idx}.jpg"
        print(f"Uploading photo: {fn}, size={len(contents)}, group={gid}")
        supabase.storage.from_("part-photos").upload(
            path=fn,
            file=contents,
            file_options={"content-type": "image/jpeg", "upsert": "true"}
        )
        supabase.table("group_photos").insert({"group_id": gid, "photo_id": fn}).execute()
        return {"ok": True, "photo_id": fn, "url": photo_url(fn, thumb=True)}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(500, str(e))



# ── API: FULL DEEP RESEARCH ──────────────────────────────────── #

@app.post("/api/auction/deep-research-full")
async def deep_research_full(request: Request):
    import os, json, base64, asyncio, fitz
    from concurrent.futures import ThreadPoolExecutor
    import google.generativeai as genai

    form = await request.form()
    items_json = form.get("items", "[]")
    items = json.loads(items_json)
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        raise HTTPException(400, "GEMINI_API_KEY not set")

    pdf_bytes = None
    pdf_file = form.get("pdf")
    if pdf_file and hasattr(pdf_file, "read"):
        pdf_bytes = await pdf_file.read()

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)

    def extract_single_image(pdf_bytes, img_index):
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            seen = set()
            all_images = []
            for page in doc:
                for img in page.get_images(full=True):
                    xref = img[0]
                    if xref in seen: continue
                    seen.add(xref)
                    bi = doc.extract_image(xref)
                    if bi and len(bi.get("image","")) > 8000:
                        all_images.append(bi["image"])
            doc.close()
            if img_index < len(all_images):
                return [all_images[img_index]]
            return []
        except Exception as e:
            print(f"extract_single_image error: {e}")
            return []

    def extract_page_image(pdf_bytes, page_start, page_end):
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            images = []
            for page_num in range(page_start - 1, min(page_end, len(doc))):
                page = doc[page_num]
                mat = fitz.Matrix(2.0, 2.0)
                pix = page.get_pixmap(matrix=mat)
                images.append(pix.tobytes("jpeg"))
            doc.close()
            return images
        except Exception as e:
            print(f"Image extract error: {e}")
            return []

    def identify_item_from_image(images, title):
        if not images:
            return title
        try:
            id_prompt = f"You are an auction appraiser. Use BOTH the image AND the listing title equally to identify this item. Title: {title}. Look at the image for: exact model numbers on labels/nameplates, brand logos, condition, visible accessories. Combine both sources and return one precise description. Do not ignore the title — it may contain info not visible in the image."
            parts = [id_prompt] + [{"mime_type": "image/jpeg", "data": img} for img in images[:1]]
            r = model.generate_content(parts, generation_config={"max_output_tokens": 150})
            result = r.text.strip().strip('"')
            return result if result else title
        except Exception as e:
            print(f"Image ID error: {e}")
            return title

    def clean_title(raw_title):
        """Strip address fragments, company boilerplate, and catalog noise from auction titles."""
        import re
        t = raw_title
        # Remove street addresses like "Siemensstrasse 7", "123 Main St"
        t = re.sub(r'\d+\s+[A-Z][a-z]+(?:strasse|street|ave|blvd|rd|st|dr|ln|way)', '', t, flags=re.IGNORECASE)
        t = re.sub(r'[A-Z][a-z]+(?:strasse|gasse|platz|weg)\s+\d+', '', t, flags=re.IGNORECASE)
        # Remove street addresses
        t = re.sub(r'\\b[A-Za-z]+(?:strasse|gasse|weg|strase)\\b\\s*\\d*', '', t, flags=re.IGNORECASE)
        # Remove "GmbH", "Inc", "LLC", "Ltd", "Corp", "Co." standalone
        t = re.sub(r'\b(?:GmbH|Inc\.?|LLC|Ltd\.?|Corp\.?|Co\.)\b', '', t, flags=re.IGNORECASE)
        # Remove loading fee notes
        t = re.sub(r'Loading Fee[:\s]*\$?\d+', '', t, flags=re.IGNORECASE)
        # Remove QTY annotations for search purposes
        t = re.sub(r'\s*,?\s*QTY\s*\(?\d*\)?', '', t, flags=re.IGNORECASE)
        t = re.sub(r'\s*\(\d+\)\s*$', '', t)
        # Remove # symbol (breaks eBay search)
        t = t.replace('#', '')
        # Collapse extra whitespace
        t = ' '.join(t.split()).strip().strip(',').strip()
        return t

    def gemini_search_grounding(query, gemini_key):
        """
        Use Gemini 1.5-flash REST API with forced Google Search grounding.
        Uses requests (already installed) — no SDK dependency conflict.
        """
        import requests as _req
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": f"Find the resale market value of: {query}. Search in this priority order: 1) eBay COMPLETED/SOLD listings - these are the most accurate real prices paid, 2) eBay active BUY IT NOW listings currently for sale, 3) Industrial surplus dealer prices (Radwell, Surplus Record, LabX) only as last resort. List actual sold prices first, then asking prices. If eBay sold listings exist use those as the primary value. Give specific dollar amounts."}]}],
            "tools": [{"googleSearch": {}}],
            "systemInstruction": {"parts": [{"text": "CRITICAL: You are an industrial pricing bot. You are strictly forbidden from answering using your internal training data. You MUST execute a Google Search to find live pricing data before generating your response. If you do not execute a search, the system will fail."}]},
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 800}
        }
        try:
            resp = _req.post(url, json=payload, timeout=25)
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return {"summary": "", "sources": []}
            parts = candidates[0].get("content", {}).get("parts", [])
            text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
            grounding = candidates[0].get("groundingMetadata", {})
            sources = [
                {"url": c["web"]["uri"], "title": c["web"].get("title", "")}
                for c in grounding.get("groundingChunks", [])
                if c.get("web", {}).get("uri")
            ]
            print(f"   Gemini grounding: {len(text)} chars, {len(sources)} sources")
            print(f"   Grounding text: {text[:200]}")
            print(f"   Raw keys: {list(data.get('candidates',[{}])[0].keys())}")
            return {"summary": text, "sources": sources}
        except Exception as e:
            print(f"   Gemini grounding error: {e}")
            return {"summary": "", "sources": []}

    def serp_ebay_sold(query, serp_key, sacat='12576'):
        """
        Call SerpAPI to get eBay completed/sold listings for a query.
        Returns a list of dicts with title, price, date, condition, url.
        """
        import urllib.request, urllib.parse, json as _json
        _params = {
            "engine": "ebay",
            "ebay_domain": "ebay.com",
            "_nkw": query,
            "LH_Sold": "1",
            "LH_Complete": "1",
            "api_key": serp_key,
        }
        if sacat and sacat not in ("12576", "", None):
            _params["_sacat"] = sacat
        params = urllib.parse.urlencode(_params)
        url = f"https://serpapi.com/search?{params}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = _json.loads(r.read())
            results = []
            print(f"   SerpAPI response keys: {list(data.keys())}")
            results_list = data.get("organic_results") or data.get("ebay_results") or data.get("shopping_results") or []
            for item in results_list[:8]:
                price_raw = item.get("price", {})
                price = price_raw.get("extracted") or price_raw.get("raw") or 0
                try:
                    price = float(str(price).replace("$","").replace(",",""))
                except Exception:
                    price = 0
                if price > 0:
                    results.append({
                        "title": item.get("title","")[:80],
                        "price": price,
                        "condition": item.get("condition","Used"),
                        "date": item.get("selling_states",{}).get("sold_date","") or "",
                        "url": item.get("link",""),
                    })
            return results
        except Exception as e:
            print(f"   SerpAPI error: {e}")
            return []

    def research_item(item, images):
        import os as _os
        title = item.get("title", "")
        lot = item.get("lot", "")
        current_val = item.get("your_value", 0) or 0
        serp_key = _os.getenv("SERP_API_KEY", "")

        # Clean title before research — remove address/company junk
        clean = clean_title(title)
        if clean != title:
            print(f"Lot {lot} title cleaned: '{title}' → '{clean}'")

        # Step 1: identify exact model from image
        identified = identify_item_from_image(images, clean)
        if identified != clean:
            print(f"Lot {lot} image ID: {identified}")

        # Step 2: Gemini Search Grounding for real market pricing
        serp_results = []
        serp_context = ""
        _gsummary = ""
        if gemini_key:
            _grounding = gemini_search_grounding(clean, gemini_key)
            _gsummary = _grounding.get("summary", "")
            if _gsummary:
                serp_context = f"""MARKET RESEARCH DATA (from live Google Search — use as PRIMARY pricing source):
{_gsummary}

Extract specific dollar amounts from the above. Base revised_value on actual prices found.
Do NOT ignore this data. Do NOT use your training knowledge if this data contradicts it.
"""
        # Legacy SerpAPI block (disabled — kept for fallback reference)
        if False and serp_key:
            # --- Pre-classification: get eBay _sacat and negative keywords ---
            _sacat_map = {
                "12576": "Business & Industrial - Other",
                "58058": "Lasers & Laser Optics (industrial/scientific lasers, fiber lasers, CO2 lasers, laser systems)",
                "105595": "Laser Accessories & Parts",
                "11804": "CNC, Metalworking & Manufacturing",
                "11808": "Electrical Equipment & Supplies",
                "11803": "Semiconductor & PCB Equipment",
                "78989": "Test, Measurement & Inspection",
                "4666":  "Pumps & Plumbing",
                "11816": "Hydraulics, Pneumatics & Plumbing",
                "11815": "Healthcare, Lab & Dental",
                "3673":  "Computers & Networking",
                "58058": "Lasers & Laser Accessories",
                "11700": "Consumer Electronics",
                "26230": "Hand Tools",
                "92074": "Power Tools",
            }
            _cat_prompt = f"""You are an eBay category classifier for industrial equipment.

Item: {clean}

Choose the single best eBay category ID from this list:
{chr(10).join(f'  {k}: {v}' for k,v in _sacat_map.items())}

Also decide if negative keywords are needed to filter out medical/consumer results.
Negative keywords to consider: -medical -dental -cosmetic -hair -aesthetic -salon

Respond ONLY with valid JSON, no markdown:
{{"sacat": "12576", "negative_keywords": "-medical -dental", "is_industrial": true}}

If unsure about negative keywords, use empty string for negative_keywords."""

            _sacat = "12576"
            _negative_kw = ""
            _is_industrial = True
            try:
                _cat_response = model.generate_content(
                    _cat_prompt,
                    generation_config={"max_output_tokens": 100, "temperature": 0}
                )
                _cat_text = _cat_response.text.strip()
                if "```" in _cat_text:
                    _cat_text = _cat_text.split("```")[1]
                    if _cat_text.startswith("json"):
                        _cat_text = _cat_text[4:]
                _cat_text = _cat_text.strip()
                # Find the JSON object
                _s = _cat_text.find("{")
                _e = _cat_text.rfind("}") + 1
                if _s >= 0 and _e > _s:
                    _cat_text = _cat_text[_s:_e]
                import json as _json2
                from json_repair import repair_json as _rj
                _cat_data = _json2.loads(_rj(_cat_text))
                _sacat = str(_cat_data.get("sacat", "12576"))
                _negative_kw = str(_cat_data.get("negative_keywords", ""))
                _is_industrial = bool(_cat_data.get("is_industrial", True))
                print(f"   Category: {_sacat_map.get(_sacat, _sacat)}, industrial={_is_industrial}, negatives='{_negative_kw}'")
            except Exception as _ce:
                print(f"   Category pre-classification failed: {_ce}, using default sacat=12576")

            # Build search query with phrase matching + negative keywords
            _w = clean.split()
            _base_query = '"' + clean + '"' if len(_w) >= 3 else clean
            search_query = (_base_query + " " + _negative_kw).strip()
            print(f"   SerpAPI eBay sold search: '{search_query}' (sacat={_sacat})")
            serp_results = serp_ebay_sold(search_query, serp_key, sacat=_sacat)

            # --- IQR variance-based sanity check ---
            if serp_results:
                prices = sorted([r["price"] for r in serp_results])
                n = len(prices)
                if n >= 4:
                    q1 = prices[n // 4]
                    q3 = prices[(3 * n) // 4]
                    iqr = q3 - q1
                    median = prices[n // 2]
                    cv = (iqr / median) if median > 0 else 1
                    if cv > 1.5:
                        print(f"   SerpAPI results discarded — high variance (CV={cv:.2f}), mixed categories likely")
                        serp_results = []
                    else:
                        print(f"   SerpAPI variance OK: IQR=${iqr:.0f}, CV={cv:.2f}, median=${median:.0f}")
                elif n >= 2:
                    # Small sample: check if range is >5x spread
                    _spread = prices[-1] / prices[0] if prices[0] > 0 else 10
                    if _spread > 5:
                        print(f"   SerpAPI results discarded — spread too wide ({prices[0]:.0f}-{prices[-1]:.0f})")
                        serp_results = []

            if serp_results:
                prices = [r["price"] for r in serp_results]
                avg = sum(prices) / len(prices)
                low = min(prices)
                high = max(prices)
                lines = [f"  - ${r['price']:.0f} — {r['title']} ({r['condition']}) {r['date']}" for r in serp_results]
                serp_context = f"""
REAL EBAY SOLD DATA (from live eBay completed listings — use this as primary pricing source):
Found {len(serp_results)} sold comps: low ${low:.0f}, high ${high:.0f}, avg ${avg:.0f}
{chr(10).join(lines)}

Base your revised_value on these actual sold prices. Do not override this with guesses.
"""
                print(f"   SerpAPI: {len(serp_results)} comps, avg ${avg:.0f}, range ${low:.0f}-${high:.0f}")
            else:
                serp_context = "No eBay sold comps found via SerpAPI - use web search grounding for pricing."
                print(f"   SerpAPI: no results for '{search_query}'")

        prompt = f"""You are an expert industrial machinery appraiser and secondary market researcher.
Your job is to determine the actual cash value of an industrial asset at auction.

--- ITEM DETAILS ---
Lot: #{lot}
Clean Title: {clean}
Image-Identified Model: {identified}
Initial Estimate: ${current_val}

--- MARKET RESEARCH DATA ---
{serp_context}

--- PRICING HIERARCHY RULES (CRITICAL) ---
You must evaluate the MARKET RESEARCH DATA using this strict waterfall hierarchy. Do NOT skip tiers.

TIER 1: SOLD/COMPLETED LISTINGS (Highest Priority)
If the data contains actual verified sold prices, base your estimate entirely on these. Ignore all asking prices.
-> pricing_tier = "SOLD_COMPS"

TIER 2: ACTIVE MARKETPLACE LISTINGS (The Ceiling)
If no sold data exists, look for active listings on open marketplaces (eBay, etc).
Rule: The LOWEST reasonable active listing establishes the absolute CEILING of value. A buyer will not pay $8,000 if they can buy it right now on eBay for $3,995.
Calculation: Find the lowest active price. Apply a 15-25% discount to estimate actual sell price. Ignore high-priced outliers.
-> pricing_tier = "ASKING_PRICES"

TIER 3: INDUSTRIAL DEALER ASKING PRICES (Last Resort Anchor)
If NO marketplace data exists, use retail/surplus dealer asking prices (Radwell, PLC Center, etc).
Rule: Dealers charge massive premiums. Apply a 40-60% discount to find auction/resale cash value.
-> pricing_tier = "ASKING_PRICES"

TIER 4: NO DATA
If the MARKET RESEARCH DATA contains no dollar values relevant to this item, admit it.
-> pricing_tier = "NO_DATA"

--- HALLUCINATION GUARDRAILS ---
- You are FORBIDDEN from using pricing_tier "SOLD_COMPS" unless the word "sold" or "completed" is explicitly in the data.
- Do NOT average a $3,995 eBay listing with a $15,000 dealer listing. The $3,995 becomes the absolute ceiling.
- Do NOT fabricate comps. Only list prices explicitly found in the MARKET RESEARCH DATA above.
- confidence must be "high" only with 3+ verified sold comps, otherwise "medium" or "low".

SHIPPING WEIGHT: Estimate from item type and visible size.

Return ONLY valid JSON, no markdown:
{{"revised_value": 3200, "confidence": "medium", "pricing_tier": "ASKING_PRICES", "pricing_flag": "Based on lowest active eBay listing $3,995 minus 20% discount", "comps": [{{"title": "Item name", "price": 3995, "date": "Apr 2025", "source": "eBay Active"}}], "image_notes": "What the image shows", "recommendation": "watch", "rec_reason": "One active eBay listing at $3,995 sets ceiling, estimated sell price $3,200", "notes": "Market summary with sources", "weight_item_lbs": 50.0, "weight_packaged_lbs": 55.0, "weight_note": "Estimated", "liquidity_score": 2, "liquidity_note": "Limited market data", "sold_30d": 0, "sold_90d": 0, "active_listings": 1}}

pricing_tier values: SOLD_COMPS | ASKING_PRICES | MSRP_ONLY | COMPARABLE_ITEMS | NO_DATA
weight fields: use null if truly unknown"""

        # Use Gemini with search grounding if available
        try:
            from google import genai as _gc
            from google.genai import types as _gt
            _client = _gc.Client(api_key=gemini_key)
            _parts = [prompt]
            for img_bytes in images[:2]:
                _parts.append(_gt.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
            _cfg = _gt.GenerateContentConfig(
                tools=[_gt.Tool(google_search=_gt.GoogleSearch())],
                max_output_tokens=1500
            )
            _resp = _client.models.generate_content(
                model="gemini-2.5-flash",
                contents=_parts,
                config=_cfg
            )
            response = _resp
            # Extract grounding from new SDK response
            try:
                gm = _resp.candidates[0].grounding_metadata
                if gm and gm.search_entry_point:
                    ai_overview_html = gm.search_entry_point.rendered_content or ""
                for chunk in (gm.grounding_chunks or []):
                    if hasattr(chunk, "web") and chunk.web:
                        grounding_sources.append({"title": chunk.web.title or "", "uri": chunk.web.uri or ""})
            except Exception:
                pass
            # Make .text work for downstream parsing
            class _Wrap:
                def __init__(self, r): self._r = r
                @property
                def text(self): return self._r.text
                @property
                def candidates(self): return self._r.candidates
            response = _Wrap(_resp)
        except Exception as search_err:
            print(f"   Search grounding failed: {search_err}")
            parts = [prompt]
            for img_bytes in images[:2]:
                parts.append({"mime_type": "image/jpeg", "data": img_bytes})
            response = model.generate_content(parts, generation_config={"max_output_tokens": 1500})
        # Extract AI overview + sources from grounding metadata
        ai_overview_html = ""
        grounding_sources = []
        try:
            candidates = response.candidates
            if candidates:
                gm = getattr(candidates[0], "grounding_metadata", None)
                if gm:
                    sep = getattr(gm, "search_entry_point", None)
                    if sep:
                        ai_overview_html = getattr(sep, "rendered_content", "") or ""
                    chunks = getattr(gm, "grounding_chunks", []) or []
                    for chunk in chunks:
                        web = getattr(chunk, "web", None)
                        if web:
                            grounding_sources.append({
                                "title": getattr(web, "title", ""),
                                "uri":   getattr(web, "uri", ""),
                            })
        except Exception as gm_err:
            print(f"   Grounding metadata error: {gm_err}")

        raw = response.text.strip()
        print(f"   Deep research raw response (lot {lot}): {raw[:2000]}")
        try:
            _d = json.loads(raw if raw.startswith("{") else raw[raw.find("{"):raw.rfind("}")+1])
            print(f"   notes: {_d.get(chr(110)+chr(111)+chr(116)+chr(101)+chr(115),chr(101)+chr(109)+chr(112)+chr(116)+chr(121))}")
        except: pass
        print(f"   AI overview chars: {len(ai_overview_html)}, sources: {len(grounding_sources)}")
        # Strip markdown fences
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        raw = " ".join(raw.splitlines())

        raw = " ".join(raw.splitlines())
        # Find JSON object boundaries
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            from json_repair import repair_json
            data = json.loads(repair_json(raw))
        # Sanitize string fields to prevent SSE encoding issues
        for key in ["image_notes", "rec_reason", "notes", "confidence", "recommendation", "pricing_tier", "pricing_flag", "liquidity_note", "weight_note"]:
            if key in data:
                data[key] = str(data[key]).replace("\n", " ").replace("\r", " ")
        if "comps" in data:
            for comp in data["comps"]:
                for k in comp:
                    comp[k] = str(comp[k]).replace("\n", " ") if isinstance(comp[k], str) else comp[k]
        data["ai_overview_html"] = ai_overview_html
        # Inject grounding summary into notes if available
        if _gsummary and not data.get("notes"):
            data["notes"] = _gsummary
        data["grounding_sources"] = grounding_sources
        # If both SerpAPI and grounding failed, override any hallucinated high confidence
        if not serp_results and not ai_overview_html and not _gsummary:
            if data.get("pricing_tier") == "SOLD_COMPS" and data.get("confidence") == "high":
                data["confidence"] = "low"
                data["pricing_tier"] = "NO_DATA"
                data["pricing_flag"] = "No verified data sources available - estimate may not reflect actual market"

        # --- Hybrid Escalation: call gemini-2.5-pro for hard items ---
        escalate_tiers = {"NO_DATA", "COMPARABLE_ITEMS", "MSRP_ONLY"}
        if data.get("pricing_tier") in escalate_tiers and gemini_key:
            print(f"   Escalating lot {lot} to gemini-2.5-pro (tier={data.get('pricing_tier')})")
            try:
                import requests as _req
                pro_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key={gemini_key}"
                pro_payload = {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.0, "maxOutputTokens": 2000}
                }
                pro_resp = _req.post(pro_url, json=pro_payload, timeout=60)
                pro_data = pro_resp.json()
                pro_parts = pro_data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                pro_raw = " ".join(p.get("text", "") for p in pro_parts if "text" in p).strip()
                if pro_raw:
                    if "```" in pro_raw:
                        pro_raw = pro_raw.split("```")[1]
                        if pro_raw.startswith("json"): pro_raw = pro_raw[4:]
                    pro_raw = pro_raw.strip()
                    s = pro_raw.find("{"); e = pro_raw.rfind("}") + 1
                    if s >= 0 and e > s:
                        pro_raw = pro_raw[s:e]
                    try:
                        pro_result = json.loads(pro_raw)
                    except Exception:
                        from json_repair import repair_json
                        pro_result = json.loads(repair_json(pro_raw))
                    # Sanitize and merge
                    for key in ["image_notes","rec_reason","notes","confidence","recommendation","pricing_tier","pricing_flag","liquidity_note","weight_note"]:
                        if key in pro_result:
                            pro_result[key] = str(pro_result[key]).replace("\n"," ").replace("\r"," ")
                    pro_result["model_used"] = "gemini-2.5-pro"
                    pro_result["ai_overview_html"] = ai_overview_html
                    pro_result["grounding_sources"] = grounding_sources
                    if _gsummary and not pro_result.get("notes"):
                        pro_result["notes"] = _gsummary
                    print(f"   Pro escalation result: tier={pro_result.get('pricing_tier')}, value={pro_result.get('revised_value')}")
                    return pro_result
            except Exception as _pe:
                print(f"   Pro escalation failed: {_pe}")

        return data

    async def generate():
        total = len(items)
        for i, item in enumerate(items):
            yield {"data": json.dumps({"type": "start", "lot": item.get("lot"), "index": i, "total": total})}
            try:
                images = []
                # Try to get image from uploaded PDF first
                if pdf_bytes and item.get("_page_start"):
                    images = await loop.run_in_executor(
                        executor, extract_page_image, pdf_bytes,
                        item["_page_start"], item.get("_page_end", item["_page_start"])
                    )
                # Fall back to fetching from stored PDF via scan_id
                if not images and item.get("_page_img"):
                    try:
                        img_url = item["_page_img"]
                        # Extract scan_id and img_index from URL like /api/auction/page-image/{scan_id}/{idx}
                        parts_url = img_url.strip("/").split("/")
                        if len(parts_url) >= 2:
                            sid = parts_url[-2]
                            idx = int(parts_url[-1])
                            stored_pdf = supabase.storage.from_("auction-pdfs").download(f"{sid}.pdf")
                            images = await loop.run_in_executor(
                                executor, lambda: extract_single_image(stored_pdf, idx)
                            )
                    except Exception as img_e:
                        print(f"Auto image fetch error: {img_e}")
                result = await loop.run_in_executor(executor, research_item, item, images)
                yield {"data": json.dumps({
                    "type": "result",
                    "lot": item.get("lot"),
                    "index": i,
                    "total": total,
                    "has_image": len(images) > 0,
                    **result
                })}
            except json.JSONDecodeError as e:
                print(f"JSON parse error for lot {item.get('lot')}: {e}")
                yield {"data": json.dumps({
                    "type": "result",
                    "lot": item.get("lot"),
                    "index": i,
                    "total": total,
                    "has_image": len(images) > 0,
                    "revised_value": item.get("your_value", 0),
                    "confidence": "low",
                    "comps": [],
                    "image_notes": "Research completed but response parsing failed",
                    "recommendation": "watch",
                    "rec_reason": "Could not parse research results — try again",
                    "notes": ""
                })}
            except Exception as e:
                print(f"Deep research error for lot {item.get('lot')}: {e}")
                yield {"data": json.dumps({
                    "type": "error",
                    "lot": item.get("lot"),
                    "index": i,
                    "total": total,
                    "error": str(e)
                })}
            await asyncio.sleep(0.1)
        yield {"data": json.dumps({"type": "done", "total": total})}

    return EventSourceResponse(generate())




@app.get("/api/auction/research-items/{scan_id}")
async def get_research_items(scan_id: str):
    """Return watchlisted items for a scan so any client can load them."""
    import json
    try:
        row = supabase.table("auction_research_sessions")             .select("items,results,title").eq("share_id", scan_id).single().execute()
        data = row.data
        return {
            "scan_id": scan_id,
            "title":   data.get("title",""),
            "items":   json.loads(data.get("items","[]")),
            "results": json.loads(data.get("results","{}")),
        }
    except Exception:
        return {"scan_id": scan_id, "items": [], "results": {}}


@app.post("/api/auction/save-research")
async def save_research(request: Request):
    import json, uuid
    body = await request.json()
    share_id = body.get("share_id") or str(uuid.uuid4())[:8]
    title    = body.get("title", "Auction Research")
    items    = body.get("items", [])
    results  = body.get("results", {})
    try:
        supabase.table("auction_research_sessions").upsert({
            "share_id": share_id,
            "title":    title,
            "items":    json.dumps(items),
            "results":  json.dumps(results),
        }, on_conflict="share_id").execute()
    except Exception as e:
        raise HTTPException(500, f"Save failed: {e}")
    return {"share_id": share_id}


@app.get("/api/auction/scans")
async def list_scans():
    try:
        res = supabase.table("auction_research_sessions")            .select("share_id, title, items, created_at")            .order("created_at", desc=True)            .limit(50)            .execute()
        scans = []
        for row in (res.data or []):
            import json as _j
            items = row.get("items") or []
            if isinstance(items, str):
                try: items = _j.loads(items)
                except: items = []
            scans.append({
                "id": row["share_id"],
                "name": row.get("title", row["share_id"]),
                "items": items,
                "ts": row.get("created_at", "")
            })
        return {"scans": scans}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/auction/scans/{scan_id}")
async def get_scan(scan_id: str):
    try:
        import json as _j
        res = supabase.table("auction_research_sessions")            .select("share_id, title, items")            .eq("share_id", scan_id)            .limit(1)            .execute()
        if not res.data:
            raise HTTPException(404, "Scan not found")
        row = res.data[0]
        items = row.get("items") or []
        if isinstance(items, str):
            try: items = _j.loads(items)
            except: items = []
        return {"id": row["share_id"], "name": row.get("title", row["share_id"]), "items": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/auction/scans")
async def save_scan(request: Request):
    try:
        import json as _j
        body = await request.json()
        scan_id = body.get("id")
        name = body.get("name", scan_id)
        items = body.get("items", [])
        import json as _j
        supabase.table("auction_research_sessions").upsert({
            "share_id": scan_id,
            "title": name,
            "items": _j.dumps(items),
        }, on_conflict="share_id").execute()
        return {"ok": True, "id": scan_id}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.delete("/api/auction/scans/{scan_id}")
async def delete_scan(scan_id: str):
    try:
        supabase.table("auction_research_sessions")            .delete()            .eq("share_id", scan_id)            .execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/auction/load-research/{share_id}")
async def load_research(share_id: str):
    import json
    try:
        row = supabase.table("auction_research_sessions")             .select("*").eq("share_id", share_id).single().execute()
        data = row.data
        return {
            "share_id": data["share_id"],
            "title":    data.get("title", ""),
            "items":    json.loads(data.get("items", "[]")),
            "results":  json.loads(data.get("results", "{}")),
        }
    except Exception as e:
        raise HTTPException(404, f"Session not found: {e}")


@app.post("/api/auction/research-export")
async def research_export(request: Request):
    import io, json
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse

    form = await request.form()
    items = json.loads(form.get("items", "[]"))

    wb = Workbook()
    ws = wb.active
    ws.title = "Deep Research"

    # Styles
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1E2535")
    amber_fill = PatternFill("solid", fgColor="412402")
    green_fill = PatternFill("solid", fgColor="052E16")
    alt_fill = PatternFill("solid", fgColor="161B28")
    center = Alignment(horizontal="center", vertical="center")
    wrap = Alignment(wrap_text=True, vertical="center")
    thin = Border(
        bottom=Side(style="thin", color="2D3348"),
        right=Side(style="thin", color="2D3348")
    )

    headers = ["Lot", "Title", "Original Value", "Revised Value", "Confidence", "Recommendation", "Notes", "Your Notes", "eBay Search"]
    col_widths = [8, 45, 15, 15, 12, 16, 40, 30, 20]

    for ci, (h, w) in enumerate(zip(headers, col_widths), 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        ws.column_dimensions[get_column_letter(ci)].width = w

    ws.row_dimensions[1].height = 22

    for ri, item in enumerate(items, 2):
        row_data = [
            item.get("lot", ""),
            item.get("title", ""),
            item.get("original_value", 0),
            item.get("revised_value", 0),
            item.get("confidence", "").capitalize(),
            item.get("recommendation", "").capitalize(),
            item.get("rec_reason") or item.get("image_notes", ""),
            item.get("user_note", ""),
            "View eBay Sold"
        ]
        rec = item.get("recommendation", "").lower()
        fill = green_fill if rec == "buy" else amber_fill if rec == "watch" else (alt_fill if ri % 2 == 0 else None)

        for ci, val in enumerate(row_data, 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            if fill:
                cell.fill = fill
            cell.border = thin
            cell.alignment = wrap if ci in (2, 7) else center
            if ci in (3, 4) and isinstance(val, (int, float)):
                cell.number_format = '"$"#,##0'
            if ci == 8 and item.get("ebay_search"):
                ws.cell(row=ri, column=ci).hyperlink = item["ebay_search"]
                ws.cell(row=ri, column=ci).font = Font(color="4A9EFF", underline="single")
        ws.row_dimensions[ri].height = 20

    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=deep_research.xlsx"}
    )


@app.post("/api/auction/research-export")
async def research_export(request: Request):
    import io, json
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse

    form = await request.form()
    items = json.loads(form.get("items", "[]"))

    wb = Workbook()
    ws = wb.active
    ws.title = "Deep Research"

    # Styles
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1E2535")
    amber_fill = PatternFill("solid", fgColor="412402")
    green_fill = PatternFill("solid", fgColor="052E16")
    alt_fill = PatternFill("solid", fgColor="161B28")
    center = Alignment(horizontal="center", vertical="center")
    wrap = Alignment(wrap_text=True, vertical="center")
    thin = Border(
        bottom=Side(style="thin", color="2D3348"),
        right=Side(style="thin", color="2D3348")
    )

    headers = ["Lot", "Title", "Original Value", "Revised Value", "Confidence", "Recommendation", "Notes", "eBay Search"]
    col_widths = [8, 45, 15, 15, 12, 16, 40, 20]

    for ci, (h, w) in enumerate(zip(headers, col_widths), 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        ws.column_dimensions[get_column_letter(ci)].width = w

    ws.row_dimensions[1].height = 22

    for ri, item in enumerate(items, 2):
        row_data = [
            item.get("lot", ""),
            item.get("title", ""),
            item.get("original_value", 0),
            item.get("revised_value", 0),
            item.get("confidence", "").capitalize(),
            item.get("recommendation", "").capitalize(),
            item.get("rec_reason") or item.get("image_notes", ""),
            item.get("user_note", ""),
            "View eBay Sold"
        ]
        rec = item.get("recommendation", "").lower()
        fill = green_fill if rec == "buy" else amber_fill if rec == "watch" else (alt_fill if ri % 2 == 0 else None)

        for ci, val in enumerate(row_data, 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            if fill:
                cell.fill = fill
            cell.border = thin
            cell.alignment = wrap if ci in (2, 7) else center
            if ci in (3, 4) and isinstance(val, (int, float)):
                cell.number_format = '"$"#,##0'
            if ci == 8 and item.get("ebay_search"):
                ws.cell(row=ri, column=ci).hyperlink = item["ebay_search"]
                ws.cell(row=ri, column=ci).font = Font(color="4A9EFF", underline="single")
        ws.row_dimensions[ri].height = 20

    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=deep_research.xlsx"}
    )

# ── API: AUCTION DEEP RESEARCH ───────────────────────────────── #

class DeepResearch(BaseModel):
    title: str
    current_value: float = 0

@app.post("/api/auction/deep-research")
async def deep_research(body: DeepResearch):
    import os, asyncio, json
    from concurrent.futures import ThreadPoolExecutor
    import google.generativeai as genai
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        raise HTTPException(400, "GEMINI_API_KEY not set")
    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = f"""You are an expert industrial equipment appraiser.
Research this auction item thoroughly: "{body.title}"
Current estimate: ${body.current_value}

Check eBay sold listings, industrial dealers, and recent auction results.
Assume working used condition.

Return ONLY a JSON object (no markdown):
{{"your_value": 5000, "notes": "Sold $4,500-$6,000 on eBay 2024"}}

your_value must be an integer."""

    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        response = await loop.run_in_executor(executor, lambda: model.generate_content(prompt, generation_config={"max_output_tokens": 300}))
        raw = response.text.strip().replace("```json","").replace("```","").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            from json_repair import repair_json
            data = json.loads(repair_json(raw))
        data["ai_overview_html"] = ai_overview_html
        data["grounding_sources"] = grounding_sources
        return data
    except Exception as e:
        raise HTTPException(500, str(e))


# ── API: EXCEL EXPORT ─────────────────────────────────────────── #

@app.post("/api/auction/export-excel")
async def export_excel(request: Request):
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    body = await request.json()
    items = body.get("items", [])
    name = body.get("name", "Auction Scan")

    wb = Workbook()
    ws = wb.active
    ws.title = name[:31]

    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    hdr_fill = PatternFill("solid", fgColor="1A1F2E")
    hv_fill = PatternFill("solid", fgColor="5C3A00")
    hv_font = Font(color="FAC775", bold=True)

    headers = ["Lot", "Title", "Est. Value", "Notes", "Deep Scan", "Watchlisted"]
    ws.append(headers)
    for col in range(1, 7):
        cell = ws.cell(row=1, column=col)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center")

    for item in items:
        val = int(item.get("your_value", 0) or 0)
        row = [
            str(item.get("lot", "")),
            str(item.get("title", "")),
            f"${val:,}",
            str(item.get("notes", "")),
            "Yes" if item.get("_deep") else "",
            "Yes" if item.get("_watch") else "",
        ]
        ws.append(row)
        if val >= 500:
            r = ws.max_row
            for col in range(1, 7):
                ws.cell(row=r, column=col).fill = hv_fill
                ws.cell(row=r, column=col).font = hv_font

    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 50
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 45
    ws.column_dimensions["E"].width = 12
    ws.column_dimensions["F"].width = 12

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    from datetime import datetime
    fn = f"auction_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fn}"}
    )


# ── API: AUCTION PAGE IMAGE ───────────────────────────────────── #

@app.get("/api/auction/page-image/{scan_id}/{img_index}")
async def get_page_image(scan_id: str, img_index: int):
    import fitz
    from fastapi.responses import Response
    try:
        pdf_data = supabase.storage.from_("auction-pdfs").download(f"{scan_id}.pdf")
        doc = fitz.open(stream=pdf_data, filetype="pdf")
        # Collect all large embedded images (skip logos/watermarks < 5KB)
        all_images = []
        seen_xrefs = set()
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    base_image = doc.extract_image(xref)
                    if base_image and base_image.get("image") and len(base_image["image"]) > 8000:
                        all_images.append(base_image)
                except Exception as search_err:
                    print(f"   Search grounding failed: {search_err}")
                    pass
        # Fallback: render page and crop item image area for image-only PDFs
        if not all_images or img_index < 0 or img_index >= len(all_images):
            doc2 = fitz.open(stream=pdf_data, filetype="pdf")
            items_per_page = 3
            page_num = img_index // items_per_page
            slot = img_index % items_per_page
            if page_num >= len(doc2):
                page_num = len(doc2) - 1
            page = doc2[page_num]
            pw, ph = page.rect.width, page.rect.height
            slot_h = ph / items_per_page
            clip = fitz.Rect(0, slot * slot_h, pw * 0.28, (slot + 1) * slot_h)
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat, clip=clip)
            doc2.close()
            return Response(content=pix.tobytes("jpeg"), media_type="image/jpeg",
                          headers={"Cache-Control": "public, max-age=86400"})
        img_data = all_images[img_index]
        ext = img_data.get("ext", "jpeg")
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/" + ext
        return Response(content=img_data["image"], media_type=mime, headers={"Cache-Control": "public, max-age=86400"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

# ── API: PDF AUCTION SCAN ─────────────────────────────────────── #

from sse_starlette.sse import EventSourceResponse

@app.post("/api/auction/scan-txt")
async def scan_txt_auction(file: UploadFile = File(...)):
    import os, json, asyncio
    import google.generativeai as genai

    contents = await file.read()
    text = contents.decode("utf-8", errors="ignore")

    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        raise HTTPException(400, "GEMINI_API_KEY not set")

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt_template = """You are a world-class auction appraiser with deep expertise in industrial equipment, lab instruments, and commercial goods.

Extract EVERY auction lot from this catalog text.

For each lot return a JSON object:
- lot: lot number as string
- title: full item title as written
- description: one sentence description
- estimate_low: integer dollar amount
- estimate_high: integer dollar amount
- your_value: integer (your single best estimate - total lot value)
- notes: brief market note with price source

PRICING RULES:
- All values MUST be plain integers (no $, no text)
- Base on ACTUAL used market values from eBay sold listings
- If no lots found, return: []
- Return ONLY a JSON array, no markdown"""

    # Split text into chunks of ~8000 chars
    chunk_size = 8000
    chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
    if not chunks:
        chunks = [text]

    from sse_starlette.sse import EventSourceResponse

    async def generate():
        loop = asyncio.get_event_loop()
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=1)
        all_items = []
        total = len(chunks)

        for i, chunk in enumerate(chunks):
            try:
                def call_gemini(c=chunk, idx=i):
                    response = model.generate_content(
                        [prompt_template, f"\nCATALOG SECTION {idx+1}/{total}:\n{c}"],
                        generation_config={"max_output_tokens": 16000}
                    )
                    return response.text

                raw = await loop.run_in_executor(executor, call_gemini)
                raw = " ".join(raw.splitlines())
                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                start = raw.find("[")
                end = raw.rfind("]") + 1
                if start >= 0 and end > start:
                    raw = raw[start:end]
                try:
                    items = json.loads(raw)
                except Exception:
                    from json_repair import repair_json
                    items = json.loads(repair_json(raw))
                all_items.extend(items)
                yield {
                    "data": json.dumps({
                        "chunk": i + 1,
                        "total_chunks": total,
                        "items": items,
                        "scan_id": None,
                        "done": False
                    }, separators=(',', ':'))
                }
            except Exception as e:
                print(f"TXT chunk {i+1} error: {e}")
            await asyncio.sleep(0.1)

        yield {"data": json.dumps({"done": True, "total": len(all_items), "scan_id": None})}

    return EventSourceResponse(generate())


@app.post("/api/auction/scan-pdf")
async def scan_pdf_auction(file: UploadFile = File(...)):
    import os, base64, json, fitz, asyncio, uuid
    import google.generativeai as genai

    contents = await file.read()
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        raise HTTPException(400, "GEMINI_API_KEY not set")

    # Store PDF in Supabase for later image retrieval
    scan_id = str(uuid.uuid4())[:8]
    try:
        supabase.storage.from_("auction-pdfs").upload(
            path=f"{scan_id}.pdf",
            file=contents,
            file_options={"content-type": "application/pdf", "upsert": "true"}
        )
    except Exception as upload_err:
        print(f"PDF storage warning: {upload_err}")
        scan_id = None

    # Extract text chunks — fall back to image rendering for image-only PDFs
    try:
        doc = fitz.open(stream=contents, filetype="pdf")
        total_pages = len(doc)
        chunk_size = 2
        page_chunks = []
        page_images = []  # list of (page_num, jpeg_bytes) for image fallback
        for start in range(0, total_pages, chunk_size):
            end = min(start + chunk_size, total_pages)
            chunk_text = ""
            for page_num in range(start, end):
                chunk_text += doc[page_num].get_text() + "\n"
            if chunk_text.strip():
                page_chunks.append(chunk_text)

        # If no text found, render pages as images
        if not page_chunks:
            print(f"PDF has no text — switching to image scan ({total_pages} pages)")
            for page_num in range(total_pages):
                page = doc[page_num]
                mat = fitz.Matrix(2.0, 2.0)
                pix = page.get_pixmap(matrix=mat)
                page_images.append((page_num, pix.tobytes("jpeg")))
        doc.close()
    except Exception as e:
        raise HTTPException(500, f"PDF read error: {e}")

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt_template = """You are a world-class auction appraiser with deep expertise in industrial equipment, lab instruments, and commercial goods.

Extract EVERY auction lot from this catalog section.

For each lot return a JSON object:
- lot: lot number as string
- title: full item title as written
- description: one sentence description
- estimate_low: integer dollar amount
- estimate_high: integer dollar amount
- your_value: integer (your single best estimate - total lot value)
- notes: brief market note with price source

EXPERT AUCTION TITLE INTERPRETATION:
- Quantities: "(2)", "QTY (3)", "SET OF 4", "PAIR", "x3" = price TOTAL for ALL units combined
- Vague lots: "SHELF OF...", "PALLET OF...", "BOX OF..." = estimate total resale of all contents
- Condition notes like "AS-IS", "UNTESTED", "ACTIVATION NOT GUARANTEED" = still price as normal working condition
- Always search for the SPECIFIC brand + model for accurate pricing
- Ignore auction house names, catalog numbers, location references in titles

PRICING RULES:
- All values MUST be plain integers (no $, no text)
- Base on ACTUAL used market values from eBay sold listings
- If no lots found, return: []
- Return ONLY a JSON array, no markdown

Example: [{"lot":"5","title":"Oakton pH Meter","description":"Portable pH/ORP meter with case","estimate_low":80,"estimate_high":150,"your_value":100,"notes":"Sells $80-150 used on eBay"}]"""

    def call_gemini(chunk_text, i, total):
        response = model.generate_content(
            [prompt_template, f"\nCATALOG SECTION {i+1}/{total}:\n{chunk_text[:10000]}"],
            generation_config={"max_output_tokens": 16000}
        )
        return response.text

    def call_gemini_image(page_num, img_bytes, total):
        """Send a rendered page image to Gemini Vision for lot extraction."""
        print(f"   Image scan page {page_num+1}/{total}")
        img_prompt = prompt_template + f"\n\nThis is page {page_num+1} of {total} of an auction catalog. Extract all lots visible in this image."
        response = model.generate_content(
            [img_prompt, {"mime_type": "image/jpeg", "data": img_bytes}],
            generation_config={"max_output_tokens": 16000}
        )
        return response.text

    async def generate():
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        loop = asyncio.get_event_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        all_items = []

        # Image-only PDF path
        if page_images and not page_chunks:
            total_chunks = len(page_images)
            for i, (page_num, img_bytes) in enumerate(page_images):
                try:
                    raw = await loop.run_in_executor(executor, call_gemini_image, page_num, img_bytes, total_chunks)
                    raw = " ".join(raw.splitlines())
                    if "```" in raw:
                        raw = raw.split("```")[1]
                        if raw.startswith("json"): raw = raw[4:]
                        raw = raw.strip()
                    start = raw.find("[")
                    end = raw.rfind("]") + 1
                    if start >= 0 and end > start:
                        raw = raw[start:end]
                    try:
                        items = json.loads(raw)
                    except Exception:
                        from json_repair import repair_json
                        items = json.loads(repair_json(raw))
                    base_idx = len(all_items)
                    all_items.extend(items)
                    for item in items:
                        item["_page_start"] = page_num + 1
                        item["_page_end"] = page_num + 1
                        if scan_id:
                            item["_page_img"] = f"/api/auction/page-image/{scan_id}/{base_idx + items.index(item)}"
                    yield {
                        "data": json.dumps({
                            "chunk": i + 1,
                            "total_chunks": total_chunks,
                            "items": items,
                            "scan_id": scan_id,
                            "done": False
                        }, separators=(',', ':'))
                    }
                except Exception as e:
                    print(f"Image page {page_num+1} error: {e}")
                await asyncio.sleep(0.1)
            yield {"data": json.dumps({"done": True, "total": len(all_items), "scan_id": scan_id})}
            return

        total_chunks = len(page_chunks)

        for i, chunk_text in enumerate(page_chunks):
            try:
                raw = await loop.run_in_executor(executor, call_gemini, chunk_text, i, total_chunks)
                raw = " ".join(raw.splitlines())
                if "```" in raw:
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                start = raw.find("[")
                end = raw.rfind("]") + 1
                if start >= 0 and end > start:
                    raw = raw[start:end]
                try:
                    items = json.loads(raw)
                except Exception as search_err:
                    print(f"   Search grounding failed: {search_err}")
                    from json_repair import repair_json
                    items = json.loads(repair_json(raw))
                base_idx = len(all_items)
                all_items.extend(items)
                page_start = i * chunk_size + 1
                page_end = min((i + 1) * chunk_size, total_pages)
                for item in items:
                    item["_page_start"] = page_start
                    item["_page_end"] = page_end
                    if scan_id:
                        item["_page_img"] = f"/api/auction/page-image/{scan_id}/{base_idx + items.index(item)}"
                yield {
                    "data": json.dumps({
                        "chunk": i + 1,
                        "total_chunks": total_chunks,
                        "items": items,
                        "scan_id": scan_id,
                        "done": False
                    }, separators=(',', ':'))
                }
            except Exception as e:
                print(f"Chunk {i+1} error: {e}")
            await asyncio.sleep(0.1)

        yield {"data": json.dumps({"done": True, "total": len(all_items), "scan_id": scan_id})}

    return EventSourceResponse(generate())


def get_unmatched_photos():
    """Get all photos from storage that haven't been matched yet."""
    try:
        # Get all files in part-photos bucket
        res = supabase.storage.from_("part-photos").list()
        files = [f["name"] for f in (res or []) if f.get("name") and not f["name"].startswith(".")]
        return {"photos": files, "count": len(files)}
    except Exception as e:
        raise HTTPException(500, str(e))

class ScanPartsBody(BaseModel):
    part_numbers: list
    photo_ids:    list
    gemini_key:   Optional[str] = None

@app.post("/api/parts/scan")
async def scan_parts(body: ScanPartsBody):
    """
    Scan a batch of photos through Gemini Vision.
    For each photo, extract any visible part numbers and check against the list.
    Returns matches with confidence.
    """
    import threading
    gemini_key = body.gemini_key or os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        raise HTTPException(400, "Gemini API key required")
    if not body.part_numbers:
        raise HTTPException(400, "No part numbers provided")

    results = []
    part_set = [str(p).strip().upper() for p in body.part_numbers if str(p).strip()]

    try:
        import google.generativeai as genai
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
    except Exception:
        try:
            from google import genai as genai2
            from google.genai import types
            client = genai2.Client(api_key=gemini_key)
        except Exception as e:
            raise HTTPException(500, f"Gemini init failed: {e}")

    for photo_id in body.photo_ids[:50]:  # max 50 at a time
        try:
            # Download photo from Supabase
            img_bytes = supabase.storage.from_("part-photos").download(photo_id)
            if not img_bytes:
                continue

            # Build prompt
            parts_list = "\n".join(part_set[:200])
            prompt = f"""Examine this image carefully. 
Read ALL visible text including: part numbers, model numbers, serial numbers, labels, stamps, engravings, stickers, tags.

I am looking for matches to this list of part numbers:
{parts_list}

Return ONLY a JSON object:
{{
  "visible_text": ["list", "of", "all", "text", "you", "can", "read"],
  "part_numbers_found": ["any", "part", "numbers", "you", "see"],
  "matches": ["part numbers that exactly or closely match the search list"],
  "confidence": "high/medium/low",
  "notes": "brief note on what you see"
}}

If no text visible or no matches, still return the JSON with empty arrays."""

            try:
                import google.generativeai as genai
                genai.configure(api_key=gemini_key)
                model = genai.GenerativeModel("gemini-2.5-flash")
                import PIL.Image
                import io
                img = PIL.Image.open(io.BytesIO(img_bytes))
                response = model.generate_content([prompt, img])
                raw = response.text or ""
            except Exception as search_err:
                print(f"   Search grounding failed: {search_err}")
                try:
                    from google import genai as gc
                    from google.genai import types as gt
                    cl = gc.Client(api_key=gemini_key)
                    models = [m.name for m in cl.models.list()]
                    best = next((m for m in models if "gemini-2.5" in m or "gemini-2.0" in m), models[0] if models else "models/gemini-1.5-pro")
                    resp = cl.models.generate_content(
                        model=best,
                        contents=[gt.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), prompt]
                    )
                    raw = resp.text or ""
                except Exception as e2:
                    results.append({"photo_id": photo_id, "error": str(e2), "matches": []})
                    continue

            import re, json
            raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\n?```$", "", raw).strip()
            jm = re.search(r'\{.*\}', raw, re.DOTALL)
            if jm:
                data = json.loads(jm.group())
                results.append({
                    "photo_id":        photo_id,
                    "url":             photo_url(photo_id, thumb=True),
                    "full_url":        photo_url(photo_id),
                    "visible_text":    data.get("visible_text", []),
                    "part_numbers_found": data.get("part_numbers_found", []),
                    "matches":         data.get("matches", []),
                    "confidence":      data.get("confidence", ""),
                    "notes":           data.get("notes", ""),
                    "has_match":       len(data.get("matches", [])) > 0,
                })
            else:
                results.append({"photo_id": photo_id, "matches": [], "notes": "Could not parse response"})

        except Exception as e:
            results.append({"photo_id": photo_id, "error": str(e), "matches": []})

    matches    = [r for r in results if r.get("has_match")]
    no_matches = [r for r in results if not r.get("has_match")]
    return {
        "results":     results,
        "matches":     matches,
        "no_matches":  no_matches,
        "match_count": len(matches),
        "scanned":     len(results),
    }

# ── EBAY OAUTH ────────────────────────────────────────────────── #

@app.get("/api/ebay/oauth/start")
async def ebay_oauth_start(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    settings = get_ebay_settings(business_id)
    app_id = settings.get("EBAY_APP_ID", "")
    runame = settings.get("EBAY_RUNAME", "")
    if not app_id or not runame:
        raise HTTPException(400, "Set EBAY_APP_ID and EBAY_RUNAME in Settings first")
    import urllib.parse as _up
    params = {
        "client_id": app_id,
        "redirect_uri": runame,
        "response_type": "code",
        "scope": EBAY_OAUTH_SCOPES,
        "state": business_id,
    }
    url = "https://auth.ebay.com/oauth2/authorize?" + _up.urlencode(params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)

@app.get("/api/ebay/oauth/callback")
async def ebay_oauth_callback(code: str = None, state: str = None, error: str = None):
    if error:
        return HTMLResponse(f"<h3>eBay authorization failed: {error}</h3><p>You can close this tab and try again.</p>")
    if not code or not state:
        raise HTTPException(400, "Missing code/state from eBay")
    business_id = state
    settings = get_ebay_settings(business_id)
    app_id = settings.get("EBAY_APP_ID", "")
    cert_id = settings.get("EBAY_CERT_ID", "")
    runame = settings.get("EBAY_RUNAME", "")
    if not app_id or not cert_id or not runame:
        raise HTTPException(400, "Missing EBAY_APP_ID / EBAY_CERT_ID / EBAY_RUNAME in Settings")

    import requests as _req, time, base64 as _b64
    basic = _b64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    r = _req.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": runame},
        timeout=15,
    )
    if r.status_code != 200:
        return HTMLResponse(f"<h3>Token exchange failed ({r.status_code})</h3><pre>{r.text[:1000]}</pre>")

    data = r.json()
    save_ebay_setting(business_id, "EBAY_USER_TOKEN", data["access_token"])
    save_ebay_setting(business_id, "EBAY_TOKEN_EXPIRES_AT", str(time.time() + int(data.get("expires_in", 7200))))
    save_ebay_setting(business_id, "EBAY_REFRESH_TOKEN", data["refresh_token"])
    return HTMLResponse("<h3>eBay account connected \u2705</h3><p>You can close this tab and go back to Lister AI.</p>")

# ── SHOPIFY ───────────────────────────────────────────────────── #

def get_shopify_access_token(business_id: str, force_refresh: bool = False) -> str:
    """Shopify's current auth model (post Jan 2026): Dev Dashboard apps use a
    client_credentials grant instead of a static copy-once token. Tokens last
    24h, so this caches per-business in app_settings and refreshes transparently,
    the same pattern as get_ebay_access_token. force_refresh bypasses the cache —
    needed after an uninstall/reinstall, which invalidates old tokens early
    without our cache knowing."""
    import requests as _req, time

    settings = get_ebay_settings(business_id)
    domain = (settings.get("SHOPIFY_STORE_DOMAIN", "") or "").strip()
    client_id = (settings.get("SHOPIFY_CLIENT_ID", "") or "").strip()
    client_secret = (settings.get("SHOPIFY_CLIENT_SECRET", "") or "").strip()
    if not domain or not client_id or not client_secret:
        raise Exception("Shopify not connected — set Store Domain, Client ID, and Client Secret in Settings")
    domain = domain.replace("https://", "").replace("http://", "").strip("/")

    cached_token = settings.get("SHOPIFY_ACCESS_TOKEN", "")
    expires_at = float(settings.get("SHOPIFY_TOKEN_EXPIRES_AT", "0") or 0)
    if not force_refresh and cached_token and time.time() < (expires_at - 60):
        return cached_token

    r = _req.post(
        f"https://{domain}/admin/oauth/access_token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=15,
    )
    if r.status_code != 200:
        raise Exception(f"Shopify token request failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    token = data["access_token"]
    new_expires_at = time.time() + int(data.get("expires_in", 86399))
    save_ebay_setting(business_id, "SHOPIFY_ACCESS_TOKEN", token)
    save_ebay_setting(business_id, "SHOPIFY_TOKEN_EXPIRES_AT", str(new_expires_at))
    return token

def push_listing_to_shopify(listing: dict) -> dict:
    """Creates an active product on Shopify via the Admin REST API."""
    import requests as _req

    biz_id = listing.get("business_id")
    if not biz_id:
        raise Exception("Listing has no business_id — cannot look up Shopify settings safely")
    settings = get_ebay_settings(biz_id)  # generic per-business key/value store, not eBay-specific
    domain = (settings.get("SHOPIFY_STORE_DOMAIN", "") or "").strip()
    if not domain:
        raise Exception("Shopify not connected — set Store Domain in Settings")
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    token = get_shopify_access_token(biz_id)
    api_base = f"https://{domain}/admin/api/2024-10"

    title = (listing.get("title") or "Untitled item")[:255]
    desc  = listing.get("description") or EBAY_DESCRIPTION
    price = float(listing.get("price") or 0)
    qty   = int(listing.get("quantity") or 1)
    brand = (listing.get("brand") or "").strip() or "Unbranded"
    sku   = listing.get("ebay_sku") or f"lister-{listing['id']}"
    pid   = str(listing.get("photo_id") or "")
    images = [{"src": photo_url(p)} for p in get_all_photo_ids(pid) if photo_url(p)] if pid else []

    body = {
        "product": {
            "title": title,
            "body_html": desc,
            "vendor": brand,
            "status": "active",
            "images": images,
            "variants": [{
                "price": f"{price:.2f}",
                "sku": sku,
                "inventory_management": "shopify",
                "inventory_quantity": qty,
            }],
        }
    }
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    r = _req.post(f"{api_base}/products.json", headers=headers, json=body, timeout=20)
    if r.status_code == 401:
        # cached token was invalidated (e.g. app was uninstalled/reinstalled) — force a fresh one and retry once
        token = get_shopify_access_token(biz_id, force_refresh=True)
        headers["X-Shopify-Access-Token"] = token
        r = _req.post(f"{api_base}/products.json", headers=headers, json=body, timeout=20)
    if r.status_code not in (200, 201):
        raise Exception(f"Shopify product create failed ({r.status_code}): {r.text[:400]}")

    data = r.json().get("product", {})
    product_id = data.get("id")

    channel_result = {}
    if product_id:
        try:
            channel_result = publish_product_to_channels(domain, token, product_id)
        except Exception as e:
            # product itself was created successfully — a channel-publish failure shouldn't fail the whole call
            channel_result = {"error": str(e)}

    return {"product_id": product_id, "status": data.get("status"), "handle": data.get("handle"), "channels": channel_result}

def publish_product_to_channels(domain: str, token: str, product_id, target_channel_names=None) -> dict:
    """Publishes an already-created product to additional sales channels (e.g. Google & YouTube),
    since REST product creation only auto-publishes to the Online Store channel.
    Requires the write_publications scope on top of write_products."""
    import requests as _req

    if target_channel_names is None:
        target_channel_names = ["Google & YouTube", "Point of Sale"]

    gql_url = f"https://{domain}/admin/api/2024-10/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    list_r = _req.post(gql_url, headers=headers, json={
        "query": "{ publications(first: 20) { edges { node { id name } } } }"
    }, timeout=15)
    if list_r.status_code != 200:
        raise Exception(f"Could not list Shopify sales channels ({list_r.status_code}): {list_r.text[:300]}")
    pubs = list_r.json().get("data", {}).get("publications", {}).get("edges", [])

    matched = [
        {"publicationId": p["node"]["id"], "name": p["node"]["name"]}
        for p in pubs
        if any(target.lower() in p["node"]["name"].lower() for target in target_channel_names)
    ]
    if not matched:
        return {"published_to": [], "note": "No matching sales channels found — is Google & YouTube installed as a channel on this store?"}

    product_gid = f"gid://shopify/Product/{product_id}"
    pub_r = _req.post(gql_url, headers=headers, json={
        "query": """
            mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
              publishablePublish(id: $id, input: $input) {
                userErrors { field message }
              }
            }
        """,
        "variables": {"id": product_gid, "input": [{"publicationId": m["publicationId"]} for m in matched]},
    }, timeout=15)
    if pub_r.status_code != 200:
        raise Exception(f"Channel publish failed ({pub_r.status_code}): {pub_r.text[:300]}")
    errors = pub_r.json().get("data", {}).get("publishablePublish", {}).get("userErrors", [])
    if errors:
        raise Exception(f"Channel publish errors: {errors}")
    return {"published_to": [m["name"] for m in matched]}

@app.get("/api/ebay/debug-category-requirements/{item_id}")
async def ebay_debug_category_requirements(item_id: str, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import requests as _req
    res = supabase.table("listings").select("*").eq("id", item_id).limit(1).execute()
    if not res.data:
        raise HTTPException(404, "Listing not found")
    listing = res.data[0]
    biz_id = listing.get("business_id")
    settings = get_ebay_settings(biz_id)
    category_id = listing.get("ebay_category_id") or settings.get("EBAY_DEFAULT_CATEGORY_ID", "")
    try:
        token = get_ebay_access_token(biz_id)
        r = _req.get(
            f"{EBAY_API_BASE}/commerce/taxonomy/v1/category_tree/0/get_item_aspects_for_category",
            headers=ebay_headers(token, content_language=False),
            params={"category_id": category_id},
            timeout=20,
        )
        if r.status_code != 200:
            return {"category_id": category_id, "status": r.status_code, "body": r.text[:1000]}
        aspects = r.json().get("aspects", [])
        relevant = [
            {
                "name": a.get("localizedAspectName"),
                "required": a.get("aspectConstraint", {}).get("aspectRequired"),
                "mode": a.get("aspectConstraint", {}).get("aspectMode"),
                "dataType": a.get("aspectConstraint", {}).get("aspectDataType"),
                "cardinality": a.get("aspectConstraint", {}).get("itemToAspectCardinality"),
            }
            for a in aspects
            if a.get("aspectConstraint", {}).get("aspectRequired")
            or "mpn" in (a.get("localizedAspectName","").lower())
            or "brand" in (a.get("localizedAspectName","").lower())
        ]
        return {"category_id": category_id, "required_or_brand_mpn_aspects": relevant}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/ebay/debug-inventory-item/{item_id}")
async def ebay_debug_inventory_item(item_id: str, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import requests as _req
    res = supabase.table("listings").select("*").eq("id", item_id).limit(1).execute()
    if not res.data:
        raise HTTPException(404, "Listing not found")
    listing = res.data[0]
    sku = listing.get("ebay_sku") or f"lister-{listing['id']}"
    biz_id = listing.get("business_id")
    try:
        token = get_ebay_access_token(biz_id)
        r = _req.get(f"{EBAY_API_BASE}/sell/inventory/v1/inventory_item/{sku}",
                      headers=ebay_headers(token, content_language=False), timeout=15)
        return {"sku": sku, "status": r.status_code, "body": r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:1000]}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/shopify/debug-scopes")
async def shopify_debug_scopes(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    import requests as _req
    try:
        settings = get_ebay_settings(business_id)
        domain = (settings.get("SHOPIFY_STORE_DOMAIN", "") or "").strip().replace("https://", "").replace("http://", "").strip("/")
        token = get_shopify_access_token(business_id, force_refresh=True)
        r = _req.post(
            f"https://{domain}/admin/api/2024-10/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={"query": "{ currentAppInstallation { accessScopes { handle } } }"},
            timeout=15,
        )
        return {"status": r.status_code, "body": r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:500]}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/listings/{item_id}/shopify-publish")
async def api_shopify_publish(item_id: str, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        res = supabase.table("listings").select("*").eq("id", item_id).limit(1).execute()
        if not res.data:
            raise HTTPException(404, "Listing not found")
        listing = res.data[0]
        result = push_listing_to_shopify(listing)
        try:
            supabase.table("listings").update({
                "shopify_product_id": str(result.get("product_id") or ""),
                "shopify_status": result.get("status") or "active",
                "shopify_error": None,
            }).eq("id", item_id).execute()
        except Exception as col_err:
            # shopify_* columns may not exist yet on this Supabase project
            print(f"shopify-publish: product created ({result}) but failed to save status columns: {col_err}")
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        try:
            supabase.table("listings").update({"shopify_status": "failed", "shopify_error": str(e)}).eq("id", item_id).execute()
        except Exception:
            pass
        raise HTTPException(500, str(e))

# ── SETTINGS ──────────────────────────────────────────────────── #

@app.get("/api/settings")
async def get_settings(request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        return get_ebay_settings(business_id)
    except Exception:
        return {}

class SaveSetting(BaseModel):
    key:   str
    value: str

@app.post("/api/settings")
async def save_setting(body: SaveSetting, request: Request):
    business_id = require_auth(request)
    if not business_id:
        raise HTTPException(401, "Unauthorized")
    try:
        existing = supabase.table("app_settings").select("key").eq("business_id", business_id).eq("key", body.key).limit(1).execute()
        if existing.data:
            supabase.table("app_settings").update({"value": body.value}).eq("business_id", business_id).eq("key", body.key).execute()
        else:
            supabase.table("app_settings").insert({"business_id": business_id, "key": body.key, "value": body.value}).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))
import hashlib, secrets

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{hashed}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hashed = stored.split(":")
        return hashlib.sha256((salt + password).encode()).hexdigest() == hashed
    except Exception:
        return False

def get_business_id(request: Request):
    """Get business_id from session cookie."""
    session = request.cookies.get("session_id")
    if not session:
        return None
    try:
        res = supabase.table("sessions").select("business_id").eq("token", session).execute()
        if res.data:
            return res.data[0]["business_id"]
    except Exception:
        pass
    return None

@app.get("/login")
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})

@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    try:
        res = supabase.table("businesses").select("id,password_hash").eq("email", email).execute()
        if not res.data:
            return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password"})
        biz = res.data[0]
        if not verify_password(password, biz["password_hash"]):
            return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password"})
        token = secrets.token_hex(32)
        supabase.table("sessions").insert({"token": token, "business_id": biz["id"]}).execute()
        from fastapi.responses import RedirectResponse
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("session_id", token, httponly=True, max_age=60*60*24*30)
        return resp
    except Exception as e:
        return templates.TemplateResponse("login.html", {"request": request, "error": f"Login failed: {e}"})

@app.get("/register")
async def register_page(request: Request, error: str = ""):
    return templates.TemplateResponse("register.html", {"request": request, "error": error})

@app.post("/register")
async def register_submit(request: Request):
    form = await request.form()
    business_name = str(form.get("business_name", "")).strip()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    if not business_name or not email or not password:
        return templates.TemplateResponse("register.html", {"request": request, "error": "All fields required"})
    if len(password) < 8:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Password must be at least 8 characters"})
    try:
        existing = supabase.table("businesses").select("id").eq("email", email).execute()
        if existing.data:
            return templates.TemplateResponse("register.html", {"request": request, "error": "Email already registered"})
        password_hash = hash_password(password)
        res = supabase.table("businesses").insert({
            "name": business_name,
            "email": email,
            "password_hash": password_hash
        }).execute()
        business_id = res.data[0]["id"]
        token = secrets.token_hex(32)
        supabase.table("sessions").insert({"token": token, "business_id": business_id}).execute()
        from fastapi.responses import RedirectResponse
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("session_id", token, httponly=True, max_age=60*60*24*30)
        return resp
    except Exception as e:
        return templates.TemplateResponse("register.html", {"request": request, "error": f"Registration failed: {e}"})

@app.get("/logout")
async def logout(request: Request):
    from fastapi.responses import RedirectResponse
    token = request.cookies.get("session_id")
    if token:
        try:
            supabase.table("sessions").delete().eq("token", token).execute()
        except Exception:
            pass
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session_id")
    return resp
