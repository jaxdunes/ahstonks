#!/usr/bin/env python3
"""
AH Ticker fetcher for WoW: Forever, using Blizzard's Game Data API.

Tracks EVERY item listed on your auction house. Blizzard only returns what's listed
right now, so each run adds one snapshot to history.json.gz, then writes:
  data.json        one row per item (price, quantity, 24h/7d change, sparkline)
  items/<id>.json  full hourly + daily history for one item (loaded when you click it)

Usage:
  export BLIZZARD_CLIENT_ID=...  BLIZZARD_CLIENT_SECRET=...
  python3 fetch_data.py --probe    # list auction houses (to find house_id)
  python3 fetch_data.py            # take a snapshot and rebuild the site data

Blizzard API terms: data is kept no longer than 30 days. Price history is trimmed to
30 days every run and item names/categories are re-fetched before they turn 30 days old.

Forever auction routes are unofficial (community-found at launch):
  /data/wow/ruleset/{ruleset}/auctions/index
  /data/wow/ruleset/{ruleset}/auctions/{house}[/commodities]
  namespace=dynamic-forever-{region}
"""
import argparse, base64, hashlib, concurrent.futures as cf, datetime as dt, email.utils, gzip, json, os, shutil, sys, time
import urllib.error, urllib.parse, urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
HIST_PATH = os.path.join(HERE, "history.json.gz")
META_PATH = os.path.join(HERE, "items_meta.json.gz")
DATA_PATH = os.path.join(HERE, "data.json")
ITEMS_DIR = os.path.join(HERE, "items")

HOURLY_KEEP_DAYS = 14
DAILY_KEEP_DAYS = 30           # Blizzard API terms: keep API data no longer than 30 days
META_REFRESH_DAYS = 29         # re-fetch item names before they reach 30 days old
META_MISS_RETRY_HOURS = 6
PER_ITEM_LOOKUPS_PER_RUN = 4000
SPARK_POINTS = 28              # 7 days at 6-hour steps
REFERENCE_ITEMS = {14047: "Runecloth", 2589: "Linen Cloth", 4306: "Silk Cloth"}
STATIC_NS = ["static-forever-{r}", "static-classic1x-{r}", "static-classic-{r}", "static-{r}"]


# ---------------- HTTP ----------------
def http(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers={
        "Accept-Encoding": "gzip", "User-Agent": "ah-ticker/3.0 (personal use)", **(headers or {})})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return r.status, {k.lower(): v for k, v in r.headers.items()}, raw
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, {k.lower(): v for k, v in e.headers.items()}, b""
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep((5 if e.code == 429 else 2) * (attempt + 1))
                continue
            body = e.read()[:300].decode("utf8", "replace")
            raise RuntimeError(f"HTTP {e.code} for {url.split('?')[0]}: {body}") from None
        except Exception as e:   # dropped connections, timeouts, truncated responses
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Network error for {url.split('?')[0]}: {e}") from None


def get_token():
    cid, sec = os.environ.get("BLIZZARD_CLIENT_ID"), os.environ.get("BLIZZARD_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("Set BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET first.")
    auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    _, _, raw = http("https://oauth.battle.net/token",
                     {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
                     b"grant_type=client_credentials")
    return json.loads(raw)["access_token"]


class Api:
    def __init__(self, region):
        self.region = region
        self.base = f"https://{region}.api.blizzard.com"
        self.token = get_token()

    def get(self, path, namespace=None, params=None, if_modified_since=None):
        url = path if path.startswith("http") else self.base + path
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        q.update(params or {})
        q["namespace"] = namespace or f"dynamic-forever-{self.region}"
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, safe="[],")))
        h = {"Authorization": f"Bearer {self.token}"}
        if if_modified_since:
            h["If-Modified-Since"] = if_modified_since
        status, headers, raw = http(url, h)
        return status, headers, (json.loads(raw) if raw else None)


def loc(x):
    """Blizzard returns names either as plain strings or as {locale: string}."""
    if isinstance(x, dict):
        return x.get("en_US") or next((v for v in x.values() if isinstance(v, str)), None)
    return x


# ---------------- auctions ----------------
def fetch_house(api, ruleset, house, ims=None):
    path = f"/data/wow/ruleset/{ruleset}/auctions/{house}"
    status, headers, body = api.get(path, if_modified_since=ims)
    if status == 304:
        return None, None, headers.get("last-modified")
    auctions = list((body or {}).get("auctions") or [])
    href = ((body or {}).get("commodities") or {}).get("href")
    try:
        _, _, cbody = api.get(href or path + "/commodities")
        commodities = (cbody or {}).get("auctions") or []
    except RuntimeError as e:
        print("commodities fetch failed:", e, file=sys.stderr)
        commodities = []
    return auctions, commodities, headers.get("last-modified")


def summarize(auctions, commodities, only=None):
    """item_id -> {min, p10, qty}. min = lowest unit price; p10 = unit price at the cheapest 10% of supply."""
    lots = defaultdict(list)
    for a in auctions or []:
        iid, qty, bo = (a.get("item") or {}).get("id"), a.get("quantity") or 1, a.get("buyout")
        if iid and bo and (only is None or iid in only):   # bid-only auctions have no buyout
            lots[iid].append((bo / qty, qty))
    for c in commodities or []:
        iid, qty, up = (c.get("item") or {}).get("id"), c.get("quantity") or 0, c.get("unit_price")
        if iid and up and (only is None or iid in only):
            lots[iid].append((up, qty))
    out = {}
    for iid, ls in lots.items():
        ls.sort()
        total = sum(q for _, q in ls)
        cut, run, p10 = total * 0.10, 0, ls[0][0]
        for p, q in ls:
            run += q
            if run >= cut:
                p10 = p
                break
        out[iid] = {"min": round(ls[0][0]), "p10": round(p10), "qty": total}
    return out


# ---------------- item names & categories ----------------
def meta_from(d):
    return {"n": loc(d.get("name")),
            "c": loc((d.get("item_class") or {}).get("name")) or "Other",
            "s": loc((d.get("item_subclass") or {}).get("name")) or "",
            "q": (d.get("quality") or {}).get("type") or "COMMON",
            "t": int(time.time())}


def resolve_meta(api, ids, meta):
    now = time.time()
    items = meta.setdefault("items", {})
    if meta.get("v") != 2:   # one-time: retry names that failed under the old lookup code
        for k in [k for k, m in items.items() if "miss" in m]:
            del items[k]
        meta["v"] = 2
    def stale(i):
        m = items.get(str(i))
        if not m:
            return True
        if "miss" in m:
            return now - m["miss"] > META_MISS_RETRY_HOURS * 3600
        return now - m["t"] > META_REFRESH_DAYS * 86400
    need = sorted(i for i in ids if stale(i))
    if not need:
        return
    print(f"looking up names for {len(need)} items")

    # find which static namespace knows Forever items (cached for a day)
    ns = meta.get("ns")
    if not ns or now - meta.get("ns_t", 0) > 86400:
        ns = None
        sample = need[0]
        for tmpl in STATIC_NS:
            cand = tmpl.format(r=api.region)
            try:
                _, _, b = api.get(f"/data/wow/item/{sample}", namespace=cand, params={"locale": "en_US"})
                if b and b.get("id") == sample:
                    ns = cand
                    break
            except RuntimeError:
                continue
        if not ns:
            ns = STATIC_NS[1].format(r=api.region)
            print("could not confirm an item-name namespace; trying", ns, file=sys.stderr)
        meta["ns"], meta["ns_t"] = ns, now
        print("item names from namespace", ns)

    found, stats = set(), defaultdict(int)
    # 1) bulk: item search, 1,000-wide blocks of IDs, following every page
    if meta.get("search_ok", True):
        for b in sorted({i // 1000 for i in need}):
            lo, hi, page, pages = b * 1000, b * 1000 + 999, 1, 1
            while page <= pages:
                try:
                    _, _, res = api.get("/data/wow/search/item", namespace=ns,
                                        params={"id": f"[{lo},{hi}]", "orderby": "id", "_pageSize": "1000", "_page": str(page)})
                except Exception as e:
                    print("item search failed, falling back to per-item lookups:", e, file=sys.stderr)
                    if not found:
                        meta["search_ok"] = False
                    break
                pages = min(int((res or {}).get("pageCount") or 1), 50)
                for r in (res or {}).get("results", []):
                    d = r.get("data") or {}
                    if d.get("id") is not None:
                        items[str(d["id"])] = meta_from(d)
                        found.add(d["id"])
                page += 1
            if not meta.get("search_ok", True):
                break
    stats["search"] = len([i for i in need if i in found])

    # 2) one by one for anything left, trying other namespaces if Forever's doesn't have it
    rest = [i for i in need if i not in found][:PER_ITEM_LOOKUPS_PER_RUN]
    others = [ns] + [t.format(r=api.region) for t in STATIC_NS if t.format(r=api.region) != ns]
    def one(i):
        not_found = 0
        for cand in others:
            try:
                _, _, d = api.get(f"/data/wow/item/{i}", namespace=cand, params={"locale": "en_US"})
                if d and d.get("id") == i:
                    return i, meta_from(d), "lookup"
            except Exception as e:
                if "HTTP 404" in str(e):
                    not_found += 1
                    continue
                return i, None, "error"     # throttled or server hiccup: retry next run
        return i, None, "notfound" if not_found == len(others) else "error"
    if rest:
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            for i, m, how in ex.map(one, rest):
                stats[how] += 1
                if m:
                    items[str(i)] = m
                elif how == "notfound":
                    items[str(i)] = {"miss": int(now)}
                else:
                    items.pop(str(i), None)     # no entry, so it's retried on the next run
    print("item names: {search} via search, {lookup} via lookup, {notfound} not found, {error} errors (retry next run)".format(**{k: stats.get(k, 0) for k in ("search", "lookup", "notfound", "error")}))
    # drop entries past the 30-day limit
    for k in [k for k, m in items.items() if "t" in m and now - m["t"] > DAILY_KEEP_DAYS * 86400]:
        del items[k]


# ---------------- probe ----------------
def probe(cfg):
    api = Api(cfg["region"])
    rs = cfg.get("ruleset", 2)
    _, _, rindex = api.get("/data/wow/ruleset/index", params={"locale": "en_US"})
    print("Rulesets:", [(r.get("id"), loc(r.get("name"))) for r in (rindex or {}).get("rulesets", [])])
    _, _, index = api.get(f"/data/wow/ruleset/{rs}/auctions/index", params={"locale": "en_US"})
    houses = (index or {}).get("auctions") or (index or {}).get("houses") or []
    print(f"\nAuction houses under ruleset {rs}:\n")
    for h in houses:
        hid, nm = h.get("id"), loc(h.get("name"))
        try:
            auctions, commodities, _ = fetch_house(api, rs, hid)
        except RuntimeError as e:
            print(f"  house {hid}: {e}")
            continue
        s = summarize(auctions, commodities, set(REFERENCE_ITEMS))
        refs = ", ".join(f"{REFERENCE_ITEMS[i]} {fmt(s[i]['min'])} x{s[i]['qty']}" for i in REFERENCE_ITEMS if i in s)
        print(f"  house {hid:>4}  {nm or '?':<24} listings={len(auctions):>6} commodities={len(commodities):>6}  {refs or '(none)'}")


def fmt(c):
    g, s, cp = c // 10000, c % 10000 // 100, c % 100
    return (f"{g}g" if g else "") + (f"{s}s" if s or g else "") + f"{cp}c"


# ---------------- history ----------------
def load_gz(path, default):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with gzip.open(path, "rt") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            print(f"could not read {os.path.basename(path)} ({e}); starting fresh", file=sys.stderr)
    return default


def save_gz(path, obj):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", compresslevel=6) as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def add_snapshot(hist, snap, summary, field):
    """History is columnar: one shared list of snapshot times, and per item aligned price/qty lists."""
    T = hist.setdefault("snapshots", [])
    items = hist.setdefault("items", {})
    if T and T[-1] >= snap:
        return False
    n = len(T)
    T.append(snap)
    for k in set(items) | {str(i) for i in summary}:
        rec = items.get(k) or {"p": [None] * n, "q": [0] * n}
        x = summary.get(int(k))
        rec["p"].append(x[field] if x else None)
        rec["q"].append(x["qty"] if x else 0)
        items[k] = rec
    # trim to the hourly window
    cut = snap - HOURLY_KEEP_DAYS * 86400
    start = next((i for i, t in enumerate(T) if t >= cut), len(T))
    if start:
        hist["snapshots"] = T[start:]
        for k in list(items):
            items[k]["p"] = items[k]["p"][start:]
            items[k]["q"] = items[k]["q"][start:]
    for k in [k for k, r in items.items() if all(p is None for p in r["p"])]:
        del items[k]
    return True


def day_of(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat()


def update_daily(hist, now_ts):
    T = hist.get("snapshots", [])
    daily = hist.setdefault("daily", {})
    if not T:
        return
    first_full_day = day_of(T[0] + 86400) if len(T) > 1 else day_of(T[0])
    oldest_allowed = (dt.datetime.fromtimestamp(now_ts, dt.timezone.utc).date()
                      - dt.timedelta(days=DAILY_KEEP_DAYS - 1)).isoformat()
    days = [day_of(t) for t in T]
    for k, rec in hist.get("items", {}).items():
        agg = defaultdict(lambda: [0, 0, 0])  # sum price, count, max qty
        for d, p, q in zip(days, rec["p"], rec["q"]):
            a = agg[d]
            if p is not None:
                a[0] += p
                a[1] += 1
            a[2] = max(a[2], q)
        old = {r[0]: r for r in daily.get(k, [])}
        for d, (sp, n, mq) in agg.items():
            if n and (d >= first_full_day or d not in old):
                old[d] = [d, round(sp / n), mq]
        daily[k] = sorted(old.values())
    for k in list(daily):
        daily[k] = [r for r in daily[k] if r[0] >= oldest_allowed]
        if not daily[k]:
            del daily[k]


# ---------------- outputs ----------------
def carry(T, P, Q):
    """Hourly points, carrying the last price forward through hours with nothing listed."""
    out, last = [], None
    for t, p, q in zip(T, P, Q):
        if p is not None:
            last = p
        if last is not None:
            out.append([t, last, q])
    return out


def price_at(points, t):
    best = None
    for pt in points:
        if pt[0] <= t:
            best = pt
        else:
            break
    return best[1] if best else None


def spark(points, now):
    if not points:
        return []
    step = 7 * 86400 / SPARK_POINTS
    out, j, last = [], 0, None
    for i in range(SPARK_POINTS + 1):
        target = now - 7 * 86400 + i * step
        while j < len(points) and points[j][0] <= target:
            last = points[j][1]
            j += 1
        if last is not None:
            out.append(last)
    return out


def write_outputs(cfg, hist, meta):
    T = hist.get("snapshots", [])
    now = T[-1] if T else int(time.time())
    if os.path.isdir(ITEMS_DIR):
        shutil.rmtree(ITEMS_DIR)   # old files go away, so nothing outlives the 30-day limit
    os.makedirs(ITEMS_DIR)
    cats, cat_idx, rows = [], {}, []
    mitems = meta.get("items", {})
    keys = set(hist.get("items", {})) | set(hist.get("daily", {}))
    for k in keys:
        rec = hist["items"].get(k)
        hourly = carry(T, rec["p"], rec["q"]) if rec else []
        daily = hist.get("daily", {}).get(k, [])
        if not hourly and not daily:
            continue
        price = hourly[-1][1] if hourly else daily[-1][1]
        qty = hourly[-1][2] if hourly else 0
        p24, p7 = price_at(hourly, now - 86400), price_at(hourly, now - 7 * 86400)
        m = mitems.get(k) or {}
        cat = f"{m.get('c', 'Unknown')}|{m.get('s', '')}"
        if cat not in cat_idx:
            cat_idx[cat] = len(cats)
            cats.append(cat)
        rows.append([int(k), m.get("n") or f"Item #{k}", cat_idx[cat], m.get("q", "COMMON"),
                     price, qty,
                     round(price / p24 - 1, 4) if p24 else None,
                     round(price / p7 - 1, 4) if p7 else None,
                     spark(hourly, now)])
        with open(os.path.join(ITEMS_DIR, f"{k}.json"), "w") as f:
            json.dump({"h": hourly, "d": daily}, f, separators=(",", ":"))
    rows.sort(key=lambda r: -r[5])
    data = {
        "realm_name": cfg.get("realm_name"), "region": cfg["region"],
        "ruleset": cfg.get("ruleset", 2), "house_id": cfg["house_id"],
        "updated": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "Blizzard Game Data API",
        "snapshots": len(T), "first_snapshot": T[0] if T else None,
        "cols": ["id", "name", "cat", "quality", "price", "qty", "c24", "c7", "spark"],
        "cats": cats, "items": rows,
    }
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote data.json ({len(rows)} items, {os.path.getsize(DATA_PATH)//1024} KB) and items/")


def load_cfg():
    if os.path.exists(CFG_PATH):
        return json.load(open(CFG_PATH))
    sys.exit("config.json not found next to fetch_data.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="list auction houses to find house_id")
    ap.add_argument("--rebuild", action="store_true", help="rewrite data.json/items from saved history only")
    args = ap.parse_args()
    cfg = load_cfg()
    if args.probe:
        return probe(cfg)
    if not cfg.get("house_id"):
        sys.exit('Set "house_id" in config.json first (run with --probe to find it).')

    hist = load_gz(HIST_PATH, {})
    meta = load_gz(META_PATH, {})
    if not args.rebuild:
        api = Api(cfg["region"])
        t0 = time.time()
        auctions, commodities, lm = fetch_house(api, cfg.get("ruleset", 2), cfg["house_id"], hist.get("last_modified"))
        if auctions is None:
            print("No new snapshot since", lm)
        else:
            try:
                snap = int(email.utils.parsedate_to_datetime(lm).timestamp())
            except Exception:
                snap = int(time.time())
            s = summarize(auctions, commodities)
            sig = hashlib.sha1(json.dumps(sorted((k, v["min"], v["qty"]) for k, v in s.items())).encode()).hexdigest()
            if sig == hist.get("last_sig"):
                print("Auction data unchanged since the last snapshot; nothing new to record")
            elif add_snapshot(hist, snap, s, cfg.get("price_field", "min")):
                hist["last_sig"] = sig
                hist["last_modified"] = lm
                print(f"snapshot {lm}: {len(auctions)} auctions + {len(commodities)} commodities "
                      f"-> {len(s)} items ({time.time()-t0:.0f}s)")
        update_daily(hist, int(time.time()))
        save_gz(HIST_PATH, hist)          # the price snapshot is safe from here on
        try:
            resolve_meta(api, [int(k) for k in hist.get("items", {})], meta)
        except Exception:
            import traceback
            print("Item name lookup hit an error; prices were still saved. Names retry next run.", file=sys.stderr)
            traceback.print_exc()
        save_gz(META_PATH, meta)
    write_outputs(cfg, hist, meta)


if __name__ == "__main__":
    main()
