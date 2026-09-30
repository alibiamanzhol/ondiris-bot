import collections
import json
import urllib.parse
import urllib.request

H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://e-ondiris.gov.kz/"}


def get(url):
    req = urllib.request.Request(url, headers=H)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except Exception as e:
        return "ERR", repr(e)[:300]


for bin_ in ["181240006529", "830115300573", "190440010464"]:
    rows, page = [], 1
    while True:
        st, d = get("https://e-ondiris.gov.kz/awp-api/registry-front?" + urllib.parse.urlencode({"page": page, "limit": 100, "bin_iin": bin_}))
        if st != 200:
            print(bin_, "ERR", st, d); break
        rows += d["data"]
        if page == 1:
            print(bin_, "meta", d["meta"])
        if not d["meta"].get("hasNextPage"):
            break
        page += 1
    act = [r for r in rows if r.get("is_active") is not False]
    print(bin_, "rows", len(rows), "active", len(act),
          "codes", len({r["product_code"] for r in rows}),
          "names", len({r["product_name"] for r in rows}),
          "code+name", len({(r["product_code"], r["product_name"]) for r in rows}),
          "regnums", collections.Counter(r["registration_number"] for r in rows).most_common(10),
          "sources", collections.Counter(r.get("data_source") for r in rows))
    dup = collections.Counter(r["product_code"] for r in rows)
    print("  dup codes", [c for c, n in dup.items() if n > 1][:5])
    names = collections.Counter(r["product_name"] for r in rows)
    print("  top names", names.most_common(5))
    for key in ["registryType=full_kz&bin=", "bin="]:
        url = f"https://e-ondiris.gov.kz/webhook/ktp/ktp-registry-manufacturers?page=1&limit=20&{key}{bin_}"
        st, d = get(url)
        print("  MANUF", key, st, json.dumps(d, ensure_ascii=False)[:1500] if st == 200 else d)
st, d = get("https://e-ondiris.gov.kz/webhook/ktp/ktp-registry-manufacturers?page=1&limit=2&registryType=full_kz")
print("FULLKZ any", st, json.dumps(d, ensure_ascii=False)[:2500] if st == 200 else d)
