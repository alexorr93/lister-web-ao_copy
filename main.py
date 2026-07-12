"""
Lister AI — FastAPI Web Dashboard
Replaces Streamlit for real-time performance.
"""
import os
import csv
import io
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
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

EBAY_DESCRIPTION = "Shipped primarily with UPS and sometimes USPS. If you have special packing or shipping needs, please send a message. This item is sold in as-is condition. The seller assumes no liability for the use, operation, or installation of this product. Due to the technical nature of this equipment, the buyer is responsible for having the item professionally inspected and installed by a certified technician prior to use."

def photo_url(photo_id: str, thumb: bool = False) -> str:
    if not photo_id or photo_id in ("", "nan", "0"):
        return ""
    if thumb:
        return f"{SUPABASE_URL}/storage/v1/render/image/public/part-photos/{photo_id}?width=500&height=500&resize=cover&quality=80"
    return f"{SUPABASE_URL}/storage/v1/object/public/part-photos/{photo_id}"

# ── EBAY INVENTORY API ───────────────────────────────────────── #
EBAY_API_BASE = "https://api.ebay.com"

EBAY_ENV_KEYS = [
    "EBAY_USER_TOKEN", "EBAY_APP_ID", "EBAY_DEV_ID", "EBAY_CERT_ID", "EBAY_RUNAME",
    "EBAY_PAYMENT_POLICY_ID", "EBAY_RETURN_POLICY_ID", "EBAY_FULFILLMENT_POLICY_ID",
    "EBAY_MERCHANT_LOCATION_KEY", "EBAY_LOCATION_ZIP", "EBAY_LOCATION_COUNTRY",
    "EBAY_DEFAULT_CATEGORY_ID",
]

EBAY_OAUTH_SCOPES = "https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.inventory"

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

def push_listing_to_ebay(listing: dict, mode: str, hours_from_now: float = None, brand_override: str = None) -> dict:
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
    images = [photo_url(pid)] if pid else []

    # 1. Create/replace inventory item
    brand = (brand_override or listing.get("brand") or "").strip()
    if not brand:
        first_word = title.split()[0].strip(",.;:-") if title else ""
        brand = first_word if first_word else "Unbranded"

    # Guess MPN: the longest alphanumeric (letters+digits) token in the title that isn't the brand —
    # usually the true part number rather than a shorter model class label
    mpn = None
    alnum_tokens = [
        w.strip(",.;:-") for w in title.split()
        if w.lower().strip(",.;:-") != brand.lower()
        and any(c.isdigit() for c in w) and any(c.isalpha() for c in w)
    ]
    if alnum_tokens:
        mpn = max(alnum_tokens, key=len)

    product_data = {
        "title": title,
        "description": desc,
        "imageUrls": images,
        "aspects": {"Brand": [brand]},
    }
    if mpn:
        product_data["mpn"] = mpn
        product_data["aspects"]["MPN"] = [mpn]

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
        if r.status_code not in (200, 201):
            raise Exception(f"createOffer failed: {r.status_code} {r.text}")
        offer_id = r.json().get("offerId")

    result = {"offer_id": offer_id, "sku": sku, "item_id": None, "status": "draft", "scheduled_at": None, "brand": brand}

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
        result = push_listing_to_ebay(listing, body.mode, body.hours_from_now, body.brand)
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
    images = [{"src": photo_url(pid)}] if pid else []

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
    return {"product_id": data.get("id"), "status": data.get("status"), "handle": data.get("handle")}

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
