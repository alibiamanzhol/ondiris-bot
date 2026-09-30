import hashlib
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OrgState:
    """Значимое состояние организации в реестре. Порядок товаров и служебные поля API не хранятся."""

    bin: str
    found: bool
    company: str = ""
    products: dict[str, str] = field(default_factory=dict)  # ключ записи -> наименование товара

    def to_json(self) -> str:
        return json.dumps(
            {"bin": self.bin, "found": self.found, "company": self.company, "products": self.products},
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "OrgState":
        d = json.loads(raw)
        return cls(bin=d["bin"], found=d["found"], company=d.get("company", ""), products=d.get("products", {}))

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

    def product_names(self) -> list[str]:
        return sorted(set(self.products.values()), key=str.casefold)


def _clean(value) -> str:
    text = " ".join(str(value or "").split())
    return "" if text in ("-", "—") else text


def state_from_rows(bin_: str, rows: list[dict]) -> OrgState:
    if not rows:
        return OrgState(bin=bin_, found=False)
    products = {}
    for r in rows:
        name = _clean(r.get("product_name"))
        if not name:
            continue
        code = _clean(r.get("product_code"))
        products[f"code:{code}" if code else f"name:{name.casefold()}"] = name
    company = next((_clean(r.get("company_name")) for r in rows if _clean(r.get("company_name"))), "")
    return OrgState(bin=bin_, found=True, company=company, products=products)


APPEARED = "appeared"
DISAPPEARED = "disappeared"
UPDATED = "updated"


@dataclass(frozen=True)
class Change:
    bin: str
    kind: str
    company: str
    old_company: str = ""
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    old_digest: str = ""
    new_digest: str = ""


def _names(products: dict[str, str], keys) -> tuple[str, ...]:
    return tuple(sorted({products[k] for k in keys}, key=str.casefold))


def diff(old: OrgState | None, new: OrgState) -> Change | None:
    """old=None — базового состояния нет: организация «появилась», только если она найдена."""
    if old is None:
        if not new.found:
            return None
        return Change(new.bin, APPEARED, new.company, added=new.product_names(), new_digest=new.digest())
    if not old.found and not new.found:
        return None
    if not old.found and new.found:
        return Change(new.bin, APPEARED, new.company, added=new.product_names(),
                      old_digest=old.digest(), new_digest=new.digest())
    if old.found and not new.found:
        return Change(new.bin, DISAPPEARED, old.company, old_digest=old.digest(), new_digest=new.digest())
    added_names = set(_names(new.products, new.products.keys() - old.products.keys()))
    removed_names = set(_names(old.products, old.products.keys() - new.products.keys()))
    replaced = added_names & removed_names  # та же позиция перерегистрирована под новым кодом
    added = tuple(sorted(added_names - replaced, key=str.casefold))
    removed = tuple(sorted(removed_names - replaced, key=str.casefold))
    renamed = bool(old.company and new.company and old.company != new.company)
    if not added and not removed and not renamed:
        return None
    return Change(
        new.bin, UPDATED, new.company,
        old_company=old.company if renamed else "",
        added=added, removed=removed,
        old_digest=old.digest(), new_digest=new.digest(),
    )
