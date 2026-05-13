#!/usr/bin/env python3
"""
RoboReseller scanner patches:
  1. Mobile toolbar redesign (desktop preserved, mobile gets clean version)
  2. Strip item title to brand/model for eBay + Google searches

Run from ~/Desktop/lister_web:
    python3 patch_scanner.py
"""

FILE = "templates/index.html"

with open(FILE, "r", encoding="utf-8") as f:
    c = f.read()

original = c
changes = []

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 1 — Replace the Current Batch toolbar block
# ─────────────────────────────────────────────────────────────────────────────

OLD_TOOLBAR = '''    <!-- Current Batch header + actions -->
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:8px;flex-wrap:wrap;">
      <div style="display:flex;align-items:baseline;gap:10px;">
        <div style="font-size:15px;font-weight:700;color:var(--text);">Current Batch</div>
        <div style="font-size:16px;color:var(--muted);" id="batch-summary">—</div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
        <select id="dash-sort" onchange="changeDashSort()" style="background:#0a0f18;border:1px solid #1a2332;border-radius:6px;color:var(--muted);font-size:15px;padding:5px 8px;cursor:pointer;font-family:inherit;outline:none;">
          <option value="batch_desc">Newest first</option>
          <option value="batch_asc">Oldest first</option>
          <option value="price_desc">Price h→l</option>
          <option value="price_asc">Price l→h</option>
          <option value="profit_desc">Profit h→l</option>
          <option value="profit_asc">Profit l→h</option>
        </select>
        <div style="display:flex;background:#0a0f18;border:1px solid #1a2332;border-radius:6px;overflow:hidden;">
          <button id="scan-view-grid" onclick="setScanView(\'grid\')" style="font-size:13px;padding:6px 12px;border:none;background:var(--accent);color:#052e16;cursor:pointer;font-family:inherit;font-weight:700;">Grid</button>
          <button id="scan-view-list" onclick="setScanView(\'list\')" style="font-size:13px;padding:6px 12px;border:none;background:transparent;color:var(--muted);cursor:pointer;font-family:inherit;">List</button>
        </div>
        <button class="btn" style="font-size:16px;padding:8px 14px;" onclick="selectAll()">Select all</button>
        <button class="btn" style="font-size:16px;padding:8px 14px;" onclick="deselectAll()">Deselect</button>
        <button class="btn" style="font-size:16px;padding:8px 14px;" onclick="openLotCostModal()" id="lot-cost-btn">Lot Cost</button>
        <button class="btn" style="font-size:16px;padding:8px 14px;background:#1a3a1a;border-color:#2d5a2d;color:#86efac;font-weight:700;" onclick="openSaveBatchModal()">Save Batch</button>
        <div style="position:relative;">
          <button class="btn" style="font-size:16px;padding:8px 14px;" onclick="toggleSavedBatchesDropdown()">📂 Saved</button>
          <div id="saved-batches-dropdown" style="display:none;position:absolute;top:100%;left:0;margin-top:4px;background:#0a0f18;border:1px solid #1a2332;border-radius:8px;padding:4px;min-width:220px;z-index:100;box-shadow:0 8px 24px rgba(0,0,0,0.5);" id="saved-batches-dd">
            <div id="saved-batches-dd-list" style="max-height:240px;overflow-y:auto;"></div>
          </div>
        </div>
        <button class="btn btn-danger" id="bulk-delete-btn" style="font-size:16px;padding:8px 14px;display:none;" onclick="bulkDeleteSelected()">Delete selected</button>
        <div style="position:relative;" id="submit-dropdown-wrap">
          <button class="btn btn-primary" id="submit-batch-btn" style="font-size:16px;padding:7px 14px;display:none;" onclick="toggleSubmitDropdown()">Submit (<span id="submit-count">0</span>) ▾</button>
          <div id="submit-dropdown" style="display:none;position:absolute;top:100%;right:0;margin-top:4px;background:#0a0f18;border:1px solid #1a2332;border-radius:8px;padding:4px;min-width:180px;z-index:50;box-shadow:0 8px 24px rgba(0,0,0,0.4);">
            <button onclick="openEbayModal();closeSubmitDropdown();" style="display:flex;align-items:center;gap:8px;width:100%;background:transparent;border:none;color:var(--text);font-size:16px;padding:8px 10px;cursor:pointer;font-family:inherit;text-align:left;border-radius:6px;">
              <span style="display:inline-block;width:7px;height:7px;background:#2563eb;border-radius:50%;"></span>
              Submit to eBay
            </button>
            <button onclick="submitToShopify();closeSubmitDropdown();" style="display:flex;align-items:center;gap:8px;width:100%;background:transparent;border:none;color:var(--text);font-size:16px;padding:8px 10px;cursor:pointer;font-family:inherit;text-align:left;border-radius:6px;">
              <span style="display:inline-block;width:7px;height:7px;background:#5a8e3b;border-radius:50%;"></span>
              Push to Shopify
            </button>
            <button onclick="window.location.href=\'/api/export/ebay-csv\';closeSubmitDropdown();" style="display:flex;align-items:center;gap:8px;width:100%;background:transparent;border:none;color:var(--text);font-size:16px;padding:8px 10px;cursor:pointer;font-family:inherit;text-align:left;border-radius:6px;">
              <span style="display:inline-block;width:7px;height:7px;background:#06b6d4;border-radius:50%;"></span>
              Export eBay CSV
            </button>
          </div>
        </div>
        <button class="btn btn-danger" style="font-size:15px;padding:5px 10px;" onclick="confirmClearBatch()">Clear</button>
      </div>
    </div>'''

NEW_TOOLBAR = '''    <!-- Current Batch header + actions -->

    <!-- DESKTOP toolbar -->
    <div class="batch-toolbar-desktop" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:8px;flex-wrap:wrap;">
      <div style="display:flex;align-items:baseline;gap:10px;">
        <div style="font-size:15px;font-weight:700;color:var(--text);">Current Batch</div>
        <div style="font-size:16px;color:var(--muted);" id="batch-summary">—</div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
        <select id="dash-sort" onchange="changeDashSort()" style="background:#0a0f18;border:1px solid #1a2332;border-radius:6px;color:var(--muted);font-size:15px;padding:5px 8px;cursor:pointer;font-family:inherit;outline:none;">
          <option value="batch_desc">Newest first</option>
          <option value="batch_asc">Oldest first</option>
          <option value="price_desc">Price h\u2192l</option>
          <option value="price_asc">Price l\u2192h</option>
          <option value="profit_desc">Profit h\u2192l</option>
          <option value="profit_asc">Profit l\u2192h</option>
        </select>
        <div style="display:flex;background:#0a0f18;border:1px solid #1a2332;border-radius:6px;overflow:hidden;">
          <button id="scan-view-grid" onclick="setScanView(\'grid\')" style="font-size:13px;padding:6px 12px;border:none;background:var(--accent);color:#052e16;cursor:pointer;font-family:inherit;font-weight:700;">Grid</button>
          <button id="scan-view-list" onclick="setScanView(\'list\')" style="font-size:13px;padding:6px 12px;border:none;background:transparent;color:var(--muted);cursor:pointer;font-family:inherit;">List</button>
        </div>
        <button class="btn" style="font-size:16px;padding:8px 14px;" onclick="selectAll()">Select all</button>
        <button class="btn" style="font-size:16px;padding:8px 14px;" onclick="deselectAll()">Deselect</button>
        <button class="btn" style="font-size:16px;padding:8px 14px;" onclick="openLotCostModal()" id="lot-cost-btn">Lot Cost</button>
        <button class="btn" style="font-size:16px;padding:8px 14px;background:#1a3a1a;border-color:#2d5a2d;color:#86efac;font-weight:700;" onclick="openSaveBatchModal()">Save Batch</button>
        <div style="position:relative;">
          <button class="btn" style="font-size:16px;padding:8px 14px;" onclick="toggleSavedBatchesDropdown()">\U0001f5c2 Saved</button>
          <div id="saved-batches-dropdown" style="display:none;position:absolute;top:100%;left:0;margin-top:4px;background:#0a0f18;border:1px solid #1a2332;border-radius:8px;padding:4px;min-width:220px;z-index:100;box-shadow:0 8px 24px rgba(0,0,0,0.5);">
            <div id="saved-batches-dd-list" style="max-height:240px;overflow-y:auto;"></div>
          </div>
        </div>
        <button class="btn btn-danger" id="bulk-delete-btn" style="font-size:16px;padding:8px 14px;display:none;" onclick="bulkDeleteSelected()">Delete selected</button>
        <div style="position:relative;" id="submit-dropdown-wrap">
          <button class="btn btn-primary" id="submit-batch-btn" style="font-size:16px;padding:7px 14px;display:none;" onclick="toggleSubmitDropdown()">Submit (<span id="submit-count">0</span>) \u25be</button>
          <div id="submit-dropdown" style="display:none;position:absolute;top:100%;right:0;margin-top:4px;background:#0a0f18;border:1px solid #1a2332;border-radius:8px;padding:4px;min-width:180px;z-index:50;box-shadow:0 8px 24px rgba(0,0,0,0.4);">
            <button onclick="openEbayModal();closeSubmitDropdown();" style="display:flex;align-items:center;gap:8px;width:100%;background:transparent;border:none;color:var(--text);font-size:16px;padding:8px 10px;cursor:pointer;font-family:inherit;text-align:left;border-radius:6px;"><span style="display:inline-block;width:7px;height:7px;background:#2563eb;border-radius:50%;"></span>Submit to eBay</button>
            <button onclick="submitToShopify();closeSubmitDropdown();" style="display:flex;align-items:center;gap:8px;width:100%;background:transparent;border:none;color:var(--text);font-size:16px;padding:8px 10px;cursor:pointer;font-family:inherit;text-align:left;border-radius:6px;"><span style="display:inline-block;width:7px;height:7px;background:#5a8e3b;border-radius:50%;"></span>Push to Shopify</button>
            <button onclick="window.location.href=\'/api/export/ebay-csv\';closeSubmitDropdown();" style="display:flex;align-items:center;gap:8px;width:100%;background:transparent;border:none;color:var(--text);font-size:16px;padding:8px 10px;cursor:pointer;font-family:inherit;text-align:left;border-radius:6px;"><span style="display:inline-block;width:7px;height:7px;background:#06b6d4;border-radius:50%;"></span>Export eBay CSV</button>
          </div>
        </div>
        <button class="btn btn-danger" style="font-size:15px;padding:5px 10px;" onclick="confirmClearBatch()">Clear</button>
      </div>
    </div>

    <!-- MOBILE toolbar -->
    <div class="batch-toolbar-mobile" style="display:none;margin-bottom:12px;background:#141419;border-radius:12px;border:1px solid rgba(255,255,255,0.07);overflow:hidden;">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:11px 14px 6px;">
        <div style="font-size:13px;font-weight:600;color:var(--text);">Current Batch</div>
        <div style="display:flex;align-items:center;gap:6px;">
          <select id="dash-sort-mobile" onchange="document.getElementById(\'dash-sort\')&&(document.getElementById(\'dash-sort\').value=this.value);changeDashSort();" style="font-size:11px;color:#888;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:4px 7px;cursor:pointer;font-family:inherit;outline:none;">
            <option value="batch_desc">Newest \u25be</option>
            <option value="batch_asc">Oldest \u25be</option>
            <option value="price_desc">Price \u2193</option>
            <option value="price_asc">Price \u2191</option>
            <option value="profit_desc">Profit \u2193</option>
            <option value="profit_asc">Profit \u2191</option>
          </select>
          <div style="position:relative;">
            <button onclick="toggleMobileOverflow(this)" style="width:28px;height:28px;border-radius:7px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);display:flex;align-items:center;justify-content:center;cursor:pointer;gap:2px;padding:0;box-sizing:border-box;">
              <span style="width:3px;height:3px;border-radius:50%;background:#888;display:inline-block;flex-shrink:0;"></span>
              <span style="width:3px;height:3px;border-radius:50%;background:#888;display:inline-block;flex-shrink:0;"></span>
              <span style="width:3px;height:3px;border-radius:50%;background:#888;display:inline-block;flex-shrink:0;"></span>
            </button>
            <div id="mobile-overflow-dd" style="display:none;position:absolute;right:0;top:34px;background:#1e1e28;border:1px solid rgba(255,255,255,0.1);border-radius:10px;min-width:160px;z-index:300;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,0.5);">
              <button onclick="openLotCostModal();closeMobileOverflow();" style="display:flex;align-items:center;gap:8px;width:100%;background:transparent;border:none;color:#ccc;font-size:13px;padding:10px 14px;cursor:pointer;font-family:inherit;text-align:left;">\U0001f4b0 Lot Cost</button>
              <button onclick="toggleSavedBatchesDropdown();closeMobileOverflow();" style="display:flex;align-items:center;gap:8px;width:100%;background:transparent;border:none;color:#ccc;font-size:13px;padding:10px 14px;cursor:pointer;font-family:inherit;text-align:left;border-top:1px solid rgba(255,255,255,0.06);">\U0001f5c2 Saved Batches</button>
            </div>
          </div>
        </div>
      </div>
      <div id="batch-summary-mobile" style="padding:0 14px 9px;font-size:11.5px;color:#555;">—</div>
      <div style="height:1px;background:rgba(255,255,255,0.05);margin:0 14px;"></div>
      <div style="display:flex;align-items:center;gap:8px;padding:9px 14px;">
        <button onclick="toggleMobileSelectMode(this)" id="mobile-select-btn" style="display:flex;align-items:center;gap:5px;font-size:12px;color:#888;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:7px 11px;cursor:pointer;font-family:inherit;">
          <span style="width:13px;height:13px;border:1.5px solid #555;border-radius:3px;display:inline-block;flex-shrink:0;" id="mobile-select-check"></span>
          Select
        </button>
        <button onclick="openSaveBatchModal()" style="display:flex;align-items:center;gap:5px;font-size:13px;font-weight:600;color:#86efac;background:rgba(34,197,94,0.12);border:1px solid rgba(34,197,94,0.25);border-radius:8px;padding:8px 16px;cursor:pointer;font-family:inherit;margin-left:auto;">\u2191 Save</button>
      </div>
      <div style="padding:0 14px 9px;">
        <button onclick="confirmClearBatch()" style="font-size:11px;color:#e05252;background:none;border:none;cursor:pointer;padding:0;font-family:inherit;">Clear batch</button>
      </div>
    </div>'''

if OLD_TOOLBAR in c:
    c = c.replace(OLD_TOOLBAR, NEW_TOOLBAR, 1)
    changes.append("✓ Replaced batch toolbar with desktop+mobile split")
else:
    changes.append("✗ Toolbar block not matched — whitespace may differ, check manually")

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 2 — Responsive CSS for toolbar visibility
# ─────────────────────────────────────────────────────────────────────────────

MOBILE_CSS = """
/* Batch toolbar responsive */
@media (max-width: 768px) {
  .batch-toolbar-desktop { display: none !important; }
  .batch-toolbar-mobile { display: block !important; }
}
@media (min-width: 769px) {
  .batch-toolbar-mobile { display: none !important; }
}
"""

if "batch-toolbar-desktop" not in c.split("<body")[0]:
    idx = c.rfind("</style>")
    if idx != -1:
        c = c[:idx] + MOBILE_CSS + c[idx:]
        changes.append("✓ Injected responsive toolbar CSS")
    else:
        changes.append("⚠ No </style> found for CSS injection")

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 3 — JS helpers + stripSearchTitle
# ─────────────────────────────────────────────────────────────────────────────

NEW_JS = r"""
// Strip item title to brand+model for cleaner eBay/Google searches
function stripSearchTitle(title) {
  if (!title) return '';
  var t = title;
  // Cut at common condition/separator markers
  t = t.replace(/\s*[-|]{1,2}\s*(used|like new|new|excellent|good|fair|poor|works|great|bundle|lot|set|oem|authentic|genuine|free\s*ship|fast\s*ship|tested|parts only|as.?is|see desc|read desc).*/gi, '');
  // Cut trailing fluff
  t = t.replace(/\s+(w\/|with\s+case|incl\.?|includes|bundle|lot|set|size\s*\d|sealed|nib|nwt|nwob|htf|vhtf|rare|great condition|clean|works great).*/gi, '');
  // Remove parentheticals
  t = t.replace(/[\(\[{][^\)\]]*[\)\]}]/g, '');
  // Clean up
  t = t.replace(/[,!?*#]+/g, ' ').replace(/\s+/g, ' ').trim();
  // Cap at 5 words
  return t.split(' ').filter(Boolean).slice(0, 5).join(' ');
}

// Mobile overflow menu
function toggleMobileOverflow(btn) {
  var dd = document.getElementById('mobile-overflow-dd');
  if (!dd) return;
  var isOpen = dd.style.display === 'block';
  dd.style.display = isOpen ? 'none' : 'block';
  if (!isOpen) {
    setTimeout(function() {
      document.addEventListener('click', function closeDd(e) {
        if (!btn.parentElement.contains(e.target)) {
          dd.style.display = 'none';
          document.removeEventListener('click', closeDd);
        }
      });
    }, 10);
  }
}
function closeMobileOverflow() {
  var dd = document.getElementById('mobile-overflow-dd');
  if (dd) dd.style.display = 'none';
}

// Mobile select toggle
var _mobileSelectActive = false;
function toggleMobileSelectMode(btn) {
  _mobileSelectActive = !_mobileSelectActive;
  var chk = document.getElementById('mobile-select-check');
  if (_mobileSelectActive) {
    btn.style.color = '#4ade80';
    btn.style.borderColor = 'rgba(34,197,94,0.3)';
    btn.style.background = 'rgba(34,197,94,0.08)';
    if (chk) { chk.style.borderColor = '#4ade80'; chk.style.background = 'rgba(34,197,94,0.15)'; }
    if (typeof selectAll === 'function') selectAll();
  } else {
    btn.style.color = '#888';
    btn.style.borderColor = 'rgba(255,255,255,0.1)';
    btn.style.background = 'rgba(255,255,255,0.05)';
    if (chk) { chk.style.borderColor = '#555'; chk.style.background = 'transparent'; }
    if (typeof deselectAll === 'function') deselectAll();
  }
}

// Keep mobile batch-summary-mobile in sync
var _origUpdateBatchSummary = null;
(function() {
  var interval = setInterval(function() {
    var bs = document.getElementById('batch-summary');
    var bsm = document.getElementById('batch-summary-mobile');
    if (bs && bsm) {
      var observer = new MutationObserver(function() { bsm.textContent = bs.textContent; });
      observer.observe(bs, {childList:true, characterData:true, subtree:true});
      bsm.textContent = bs.textContent;
      clearInterval(interval);
    }
  }, 300);
})();

"""

if "stripSearchTitle" not in c:
    anchor = "function setScanView"
    if anchor in c:
        c = c.replace(anchor, NEW_JS + "function setScanView", 1)
        changes.append("✓ Injected stripSearchTitle() + mobile toolbar JS")
    else:
        changes.append("⚠ JS anchor 'function setScanView' not found")
else:
    changes.append("⚠ stripSearchTitle already present — skipped")

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 4 — Wrap search title args with stripSearchTitle()
# ─────────────────────────────────────────────────────────────────────────────

search_patches = [
    (
        """encodeURIComponent(document.querySelector('.tile-title-input[data-id="${tid}"]').value),'popupwin'""",
        """encodeURIComponent(stripSearchTitle(document.querySelector('.tile-title-input[data-id="${tid}"]').value)),'popupwin'"""
    ),
    (
        """encodeURIComponent(document.querySelector('.tile-title-input[data-id="${l.id}"]').value),'popupwin'""",
        """encodeURIComponent(stripSearchTitle(document.querySelector('.tile-title-input[data-id="${l.id}"]').value)),'popupwin'"""
    ),
    (
        "encodeURIComponent(item.title || '')",
        "encodeURIComponent(stripSearchTitle(item.title || ''))"
    ),
]

for old, new in search_patches:
    n = c.count(old)
    if n:
        c = c.replace(old, new)
        changes.append(f"✓ Patched search query ({n}x): {old[-45:]}")
    else:
        changes.append(f"⚠ Not found: {old[-45:]}")

# ─────────────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────────────
if c != original:
    with open(FILE, "w", encoding="utf-8") as f:
        f.write(c)
    print("✅ File written.\n")
else:
    print("⚠ No changes made.\n")

print("── Patch report ──")
for msg in changes:
    print(" ", msg)
