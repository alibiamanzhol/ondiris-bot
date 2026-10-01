import collections, json, urllib.parse, urllib.request
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://e-ondiris.gov.kz/"}
def get(params):
    url = "https://e-ondiris.gov.kz/awp-api/registry-front?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=90) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"err": repr(e)[:200]}
def fetch(bin_, limit):
    rows, page = [], 1
    while True:
        d = get({"page": page, "limit": limit, "bin_iin": bin_})
        if "err" in d: return None, d["err"]
        rows += d["data"]
        if not d["meta"].get("hasNextPage"): return rows, d["meta"]
        page += 1
def key(r): return (r["registration_number"], r["product_code"], r["product_name"], r["is_active"])
B = "181240006529"
for limit in (100, 500, 1000, 5000):
    rows, meta = fetch(B, limit)
    print("limit", limit, "->", "ERR "+str(meta) if rows is None else f"rows={len(rows)} meta_total={meta.get('total')} limit_back={meta.get('limit')} pages={meta.get('totalPages')} unique={len(set(map(key, rows)))}")
runs = [fetch(B, 100)[0] for _ in range(3)]
sets = [collections.Counter(map(key, r)) for r in runs]
print("stable limit100:", sets[0] == sets[1] == sets[2], [len(s) for s in sets], [sum(s.values()) for s in sets])
print("diff01", len(sets[0] - sets[1]), len(sets[1] - sets[0]))
full = [r for r in runs[0]]
c = collections.Counter(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in full)
print("fully identical duplicate rows (all fields):", sum(n - 1 for n in c.values() if n > 1))
big = fetch(B, 1000)[0]
if big:
    bigs = [collections.Counter(map(key, fetch(B, 1000)[0])) for _ in range(2)] + [collections.Counter(map(key, big))]
    print("stable limit1000:", bigs[0] == bigs[1] == bigs[2], [len(s) for s in bigs])
    cb = collections.Counter(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in big)
    print("limit1000 identical dup rows:", sum(n - 1 for n in cb.values() if n > 1), "unique keys", len(set(map(key, big))))
