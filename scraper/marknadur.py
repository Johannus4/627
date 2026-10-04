#!/usr/bin/env python3
"""
marknadur.py — dagligur scrapari fyri bugv.fo og boligsiden.dk

Bygt við fleiri atferðum í rað, tí sum síðurnar eru bygdar broytist:
  1) Egna JSON-API-ið hjá síðuni (best, um tað finst)
  2) JSON-LD (schema.org) í HTML-inum — tað flestu ogn-síður leggja út
  3) __NEXT_DATA__ / Nuxt-state fyri React/Vue-síður
  4) Generisk HTML-heuristikk sum síðsta loysn

Historikkur verður goymdur í history.json, so LIGGITÍÐ kann roknast:
fyrstu ferð vit síggja eina annonsu, skriva vit dagin niður.

Koyr:
  python marknadur.py            # vanligt — skrivar marknadur.json
  python marknadur.py --probe    # vísir hvat síðurnar í roynd og veru svara
"""
import json, re, sys, time, hashlib, datetime as dt
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
HEAD = {"User-Agent": UA, "Accept-Language": "fo,da;q=0.9,en;q=0.8"}
TODAY = dt.date.today().isoformat()
OUT = Path("marknadur.json")
HIST = Path("history.json")

def get(url, **kw):
    kw.setdefault("timeout", 30)
    kw.setdefault("headers", HEAD)
    return requests.get(url, **kw)

def digits(v):
    """'2.450.000 kr' -> 2450000 ; '145 m2' -> 145"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d.,\s]*", str(v))
    if not m:
        return None
    s = m.group(0).replace(" ", "")
    s = re.sub(r"\.(?=\d{3}(\D|$))", "", s)
    s = re.sub(r",(?=\d{3}(\D|$))", "", s)
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None

def row(**k):
    k.setdefault("source", "")
    k.setdefault("url", None)
    k.setdefault("market", "fo")
    r = {
        "title": (k.get("title") or "").strip() or "—",
        "place": (k.get("place") or "").strip() or "—",
        "type":  (k.get("type") or "").strip() or "—",
        "price": digits(k.get("price")),
        "area":  digits(k.get("area")),
        "rooms": digits(k.get("rooms")),
        "url":   k.get("url"),
        "source": k["source"],
        "market": k["market"],
    }
    r["id"] = hashlib.sha1(
        (r["source"] + "|" + (r["url"] or r["title"] + r["place"])).encode()
    ).hexdigest()[:16]
    return r

# ---------------------------------------------------------------
# 2) JSON-LD — virkar á ótrúliga nógvum síðum
# ---------------------------------------------------------------
def from_jsonld(html, source, base, market='fo'):
    out, soup = [], BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node); continue
            if not isinstance(node, dict):
                continue
            for key in ("itemListElement", "@graph", "mainEntity"):
                if key in node:
                    stack.append(node[key])
            t = str(node.get("@type", ""))
            if not re.search(r"Residence|Apartment|House|Product|Offer|RealEstate", t, re.I):
                continue
            offer = node.get("offers") or {}
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            addr = node.get("address") or {}
            if isinstance(addr, str):
                place = addr
            else:
                place = addr.get("addressLocality") or addr.get("addressRegion") or ""
            u = node.get("url") or offer.get("url")
            out.append(row(
                title=node.get("name") or node.get("streetAddress"),
                place=place,
                type=t,
                price=offer.get("price") or node.get("price"),
                area=(node.get("floorSize") or {}).get("value")
                     if isinstance(node.get("floorSize"), dict) else node.get("floorSize"),
                rooms=node.get("numberOfRooms"),
                url=urljoin(base, u) if u else None,
                source=source, market=market))
    return out

# ---------------------------------------------------------------
# 3) __NEXT_DATA__ / Nuxt — React/Vue-síður
# ---------------------------------------------------------------
PRICE_K = ("price", "prisur", "pris", "askingprice", "kostur", "upphaedd")
AREA_K  = ("area", "m2", "sqm", "size", "stodd", "livingarea", "boligareal")
PLACE_K = ("city", "town", "place", "bygd", "stadur", "kommuna", "postaltown")

def from_embedded_json(html, source, base, market='fo'):
    out = []
    for pat in (r'__NEXT_DATA__[^>]*>(.*?)</script>',
                r'window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>',
                r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>'):
        m = re.search(pat, html, re.S)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        stack, seen = [data], 0
        while stack and seen < 200000:
            seen += 1
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node); continue
            if not isinstance(node, dict):
                continue
            low = {str(k).lower(): v for k, v in node.items()}
            has_price = any(k in low for k in PRICE_K)
            has_area = any(k in low for k in AREA_K)
            if has_price and (has_area or "address" in low or "title" in low):
                g = lambda keys: next((low[k] for k in keys if k in low), None)
                u = low.get("url") or low.get("slug") or low.get("link")
                out.append(row(
                    title=low.get("title") or low.get("name") or low.get("address"),
                    place=g(PLACE_K), type=low.get("type") or low.get("category"),
                    price=g(PRICE_K), area=g(AREA_K),
                    rooms=low.get("rooms") or low.get("bedrooms"),
                    url=urljoin(base, str(u)) if u else None,
                    source=source, market=market))
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        if out:
            break
    return out

# ---------------------------------------------------------------
# 4) Generisk HTML — leitar eftir endurtiknum kortum við prísi
# ---------------------------------------------------------------
PRICE_RE = re.compile(r"(?:kr|DKK)\s*[\d.\s]{6,}|[\d.]{7,}\s*(?:kr|DKK)", re.I)
AREA_RE  = re.compile(r"(\d{2,4})\s*m[²2]", re.I)

def from_html(html, source, base, market='fo'):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        txt = " ".join(a.get_text(" ", strip=True).split())
        if len(txt) < 12 or not PRICE_RE.search(txt):
            continue
        area = AREA_RE.search(txt)
        out.append(row(
            title=txt[:90],
            price=PRICE_RE.search(txt).group(0),
            area=area.group(1) if area else None,
            url=urljoin(base, a["href"]),
            source=source, market=market))
    # strika tvífaldar
    seen, uniq = set(), []
    for r in out:
        if r["id"] in seen:
            continue
        seen.add(r["id"]); uniq.append(r)
    return uniq

# ---------------------------------------------------------------
# KELDUR — legg eina nýggja til við at seta eina linju inn her
# ---------------------------------------------------------------
# market: "fo" ella "dk". paths: síður at royna, í raðfylgju.
SOURCES = [
    {"name": "bugv.fo",      "market": "fo", "base": "https://bugv.fo",
     "paths": ["/", "/ognir", "/til-solu", "/sok", "/search"]},
    {"name": "bustad.fo",    "market": "fo", "base": "https://bustad.fo",
     "paths": ["/", "/ognir", "/til-solu", "/sok"]},
    {"name": "betriheim.fo", "market": "fo", "base": "https://betriheim.fo",
     "paths": ["/", "/ognir", "/til-solu"]},
    {"name": "skyn.fo",      "market": "fo", "base": "https://skyn.fo",
     "paths": ["/", "/ognir", "/til-solu"]},
    {"name": "meklarin.fo",  "market": "fo", "base": "https://meklarin.fo",
     "paths": ["/", "/ognir"]},
]

def scrape_generic(src):
    """Royn hvørja síðu við JSON-LD, innbygdum JSON og síðani HTML."""
    rows, notes = [], []
    for path in src["paths"]:
        try:
            r = get(src["base"] + path)
        except Exception as e:
            notes.append(f"{path}: {e}"); continue
        notes.append(f"{path}: HTTP {r.status_code}, {len(r.content)} b")
        if r.status_code != 200:
            continue
        for fn in (from_jsonld, from_embedded_json, from_html):
            got = fn(r.text, src["name"], src["base"], src["market"])
            if got:
                notes.append(f"   -> {fn.__name__}: {len(got)} annonsur")
                rows.extend(got)
                break
        if rows:
            break
    return rows, notes

def scrape_source(src):
    return scrape_generic(src)

# ---------------------------------------------------------------
# Historikkur -> liggitíð
# ---------------------------------------------------------------
def apply_history(rows):
    hist = {}
    if HIST.exists():
        try:
            hist = json.loads(HIST.read_text())
        except Exception:
            hist = {}
    for r in rows:
        h = hist.setdefault(r["id"], {"first_seen": TODAY, "prices": []})
        h["last_seen"] = TODAY
        if r["price"] and (not h["prices"] or h["prices"][-1][1] != r["price"]):
            h["prices"].append([TODAY, r["price"]])
        r["first_seen"] = h["first_seen"]
        r["days_on_market"] = (dt.date.fromisoformat(TODAY)
                               - dt.date.fromisoformat(h["first_seen"])).days
        if len(h["prices"]) > 1:
            first = h["prices"][0][1]
            r["price_change"] = r["price"] - first
            r["price_change_pct"] = round((r["price"] - first) / first * 100, 1) if first else None
    HIST.write_text(json.dumps(hist, ensure_ascii=False, indent=1))
    return rows

# ---------------------------------------------------------------
def main():
    probe = "--probe" in sys.argv
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--only="):
            only = a.split("=", 1)[1]

    all_rows, report = [], {}
    for src in SOURCES:
        if only and only != src["name"]:
            continue
        try:
            rows, notes = scrape_source(src)
        except Exception as e:
            rows, notes = [], [f"BRESTUR: {e}"]
        report[src["name"]] = {"market": src["market"], "count": len(rows), "notes": notes}
        all_rows.extend(rows)
        print(f"\n### {src['name']} ({src['market']}): {len(rows)} annonsur")
        for n in notes:
            print("   ", n)
        time.sleep(1)

    fo = sum(1 for r in all_rows if r["market"] == "fo")
    dk = sum(1 for r in all_rows if r["market"] == "dk")
    print(f"\n=== Tilsamans: {len(all_rows)} ({fo} FO / {dk} DK) ===")

    if probe:
        print("\n--- dømi ---")
        for r in all_rows[:6]:
            print(json.dumps(r, ensure_ascii=False))
        return 0

    all_rows = apply_history(all_rows)
    OUT.write_text(json.dumps({
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "count": len(all_rows), "count_fo": fo, "count_dk": dk,
        "sources": report,
        "listings": all_rows,
    }, ensure_ascii=False, indent=1))
    print(f"\nSkrivaði {OUT} við {len(all_rows)} annonsum.")
    return 0 if all_rows else 1

if __name__ == "__main__":
    sys.exit(main())

