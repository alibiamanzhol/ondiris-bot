import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class OrgState:
    """Значимое состояние организации в реестре.

    records: ключ записи → (наименование товара, активна ли запись). Записью считается строка таблицы
    реестра — так же, как на сайте («Показано 1-10 из N»). Порядок строк и служебные поля не хранятся.
    """

    bin: str
    found: bool
    company: str = ""
    records: dict[str, tuple[str, bool]] = field(default_factory=dict)
    total: int = 0
    legacy: bool = field(default=False, compare=False)

    @property
    def active(self) -> int:
        return sum(1 for _, is_active in self.records.values() if is_active)

    def product_names(self) -> list[str]:
        return sorted({name for name, _ in self.records.values()}, key=str.casefold)

    def to_json(self) -> str:
        return json.dumps(
            {"v": SCHEMA_VERSION, "bin": self.bin, "found": self.found, "company": self.company,
             "total": self.total, "records": {k: [n, a] for k, (n, a) in self.records.items()}},
            ensure_ascii=False, sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "OrgState":
        d = json.loads(raw)
        if d.get("v") != SCHEMA_VERSION:
            # снимок прежней версии: ключи товаров другие, поэтому сравнивать товары с ним нельзя
            products = d.get("products", {})
            return cls(bin=d["bin"], found=d["found"], company=d.get("company", ""),
                       records={k: (v, True) for k, v in products.items()}, total=len(products), legacy=True)
        return cls(bin=d["bin"], found=d["found"], company=d.get("company", ""), total=d.get("total", 0),
                   records={k: (v[0], bool(v[1])) for k, v in d.get("records", {}).items()})

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]


def _clean(value) -> str:
    text = " ".join(str(value or "").split())
    return "" if text in ("-", "—") else text


def state_from_rows(bin_: str, rows: list[dict]) -> OrgState:
    if not rows:
        return OrgState(bin=bin_, found=False)
    parsed = []
    for r in rows:
        name = _clean(r.get("product_name")) or "(без наименования)"
        key = "|".join((_clean(r.get("registration_number")), _clean(r.get("product_code")), name.casefold()))
        parsed.append((key, name, r.get("is_active") is not False))
    # На портале встречаются полностью одинаковые строки — нумеруем их, чтобы каждая строка сайта была записью.
    # Сортировка делает нумерацию независимой от порядка строк в ответе.
    records, seen = {}, Counter()
    for key, name, active in sorted(parsed, key=lambda x: (x[0], not x[2])):
        seen[key] += 1
        records[f"{key}#{seen[key]}" if seen[key] > 1 else key] = (name, active)
    company = next((_clean(r.get("company_name")) for r in rows if _clean(r.get("company_name"))), "")
    return OrgState(bin=bin_, found=True, company=company, records=records, total=len(rows))


APPEARED = "appeared"
DISAPPEARED = "disappeared"
UPDATED = "updated"


@dataclass(frozen=True)
class Change:
    bin: str
    kind: str
    company: str
    total: int = 0
    active: int = 0
    old_company: str = ""
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    added_count: int = 0
    removed_count: int = 0
    became_active: int = 0
    became_inactive: int = 0
    old_digest: str = ""
    new_digest: str = ""


def _sorted(names) -> tuple[str, ...]:
    return tuple(sorted(set(names), key=str.casefold))


def diff(old: OrgState | None, new: OrgState) -> Change | None:
    """old=None — базового состояния нет: организация «появилась», только если она найдена."""
    base = dict(bin=new.bin, total=new.total, active=new.active, new_digest=new.digest())
    if old is None or (not old.found and new.found):
        if not new.found:
            return None
        return Change(kind=APPEARED, company=new.company, added=_sorted(new.product_names()),
                      added_count=new.total, old_digest=old.digest() if old else "", **base)
    if not new.found:
        if not old.found:
            return None
        return Change(kind=DISAPPEARED, company=old.company, old_digest=old.digest(), **base)
    if old.legacy:
        return None  # снимок старого формата молча заменяется новым

    added_keys = new.records.keys() - old.records.keys()
    removed_keys = old.records.keys() - new.records.keys()
    common = new.records.keys() & old.records.keys()
    became_active = sum(1 for k in common if new.records[k][1] and not old.records[k][1])
    became_inactive = sum(1 for k in common if old.records[k][1] and not new.records[k][1])
    # Запись с тем же наименованием, перерегистрированная под другим номером/кодом, изменением не считается.
    added_by_name = Counter(new.records[k][0] for k in added_keys)
    removed_by_name = Counter(old.records[k][0] for k in removed_keys)
    net_added = added_by_name - removed_by_name
    net_removed = removed_by_name - added_by_name
    renamed = bool(old.company and new.company and old.company != new.company)
    if not (net_added or net_removed or became_active or became_inactive or renamed):
        return None
    return Change(
        kind=UPDATED, company=new.company,
        old_company=old.company if renamed else "",
        added=_sorted(net_added), removed=_sorted(net_removed),
        added_count=sum(net_added.values()), removed_count=sum(net_removed.values()),
        became_active=became_active, became_inactive=became_inactive,
        old_digest=old.digest(), **base,
    )
