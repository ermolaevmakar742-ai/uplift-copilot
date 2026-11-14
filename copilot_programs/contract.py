"""Partner program model + validation. Pure: stdlib only (no main.py, no DB, no network).

A Program is an immutable, validated record. Validation rules (all errors are collected, field -> message):
  program_id         slug [a-z0-9][a-z0-9_-]{1,63}, immutable after creation
  name, brand        non-empty text <= 120
  countries          list of ISO-3166 alpha-2 codes (A-Z), de-duplicated and sorted
  currency           ISO-4217 alpha-3 (A-Z), upper-cased
  registration_url   http(s) URL with a host, no spaces/control characters, <= 2048
  promo_code         optional text <= 64 (a promo code is public, not a secret)
  min_deposit / min_withdrawal   optional number >= 0, <= 1e9, at most 2 decimals (never silently rounded)
  withdrawal_time, support_contact  optional text <= 120 / 255
  payment_methods    list of non-empty strings <= 60 each, at most 30, de-duplicated (order kept)
  terms, notes       optional free text <= 4000, <= 2000
  active             bool. An ACTIVE program must have: name, brand, registration_url.
  updated_by         non-empty text <= 64
Nothing is invented: absent optional values stay None / empty.
"""
import math
import re
from dataclasses import dataclass, fields, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit

CONTRACT_VERSION = "program.v1"
REQUIRED_WHEN_ACTIVE = ("name", "brand", "registration_url")      # countries/currency are optional metadata
EDITABLE = ("name", "brand", "countries", "currency", "registration_url", "promo_code", "min_deposit",
            "min_withdrawal", "withdrawal_time", "payment_methods", "terms", "support_contact", "notes")

_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")
_CC = re.compile(r"[A-Z]{2}")
_CUR = re.compile(r"[A-Z]{3}")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")          # control chars (newline/tab allowed in free text only)
_MAX_MONEY = Decimal("1000000000")
_TWO = Decimal("0.01")


class ProgramValidationError(ValueError):
    def __init__(self, errors: Mapping[str, str]):
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


@dataclass(frozen=True)
class Program:
    program_id: str
    name: str
    brand: str
    countries: tuple[str, ...]
    currency: str | None
    registration_url: str | None
    promo_code: str | None
    min_deposit: Decimal | None
    min_withdrawal: Decimal | None
    withdrawal_time: str | None
    payment_methods: tuple[str, ...]
    terms: str | None
    support_contact: str | None
    notes: str | None
    active: bool
    created_at: datetime
    updated_at: datetime
    updated_by: str
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["countries"] = list(self.countries)
        d["payment_methods"] = list(self.payment_methods)
        d["min_deposit"] = None if self.min_deposit is None else float(self.min_deposit)
        d["min_withdrawal"] = None if self.min_withdrawal is None else float(self.min_withdrawal)
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        return d


def _text(v: Any, field: str, errors: dict, *, max_len: int, required=False, multiline=False) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            errors[field] = "required"
        return None
    if not isinstance(v, str):
        errors[field] = "must be text"
        return None
    v = v.strip()
    if len(v) > max_len:
        errors[field] = f"too long (max {max_len})"
    elif (_CTRL if multiline else re.compile(r"[\x00-\x1f\x7f]")).search(v):
        errors[field] = "contains control characters"
    else:
        return v
    return None


def _money(v: Any, field: str, errors: dict) -> Decimal | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
        errors[field] = "must be a number"
        return None
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        errors[field] = "must be a number"
        return None
    if not d.is_finite():
        errors[field] = "must be a finite number"
    elif d < 0:
        errors[field] = "must be >= 0"
    elif d > _MAX_MONEY:
        errors[field] = "too large"
    elif d != d.quantize(_TWO):
        errors[field] = "at most 2 decimals"
    else:
        return d.quantize(_TWO)
    return None


def _url(v: Any, errors: dict) -> str | None:
    s = _text(v, "registration_url", errors, max_len=2048)
    if s is None:
        return None
    if re.search(r"\s", s):
        errors["registration_url"] = "must not contain spaces"
        return None
    try:
        p = urlsplit(s)
        host = p.hostname
    except ValueError:
        errors["registration_url"] = "invalid URL"
        return None
    if p.scheme not in ("http", "https") or not host or "." not in host or p.username or p.password:
        errors["registration_url"] = "must be an http(s) URL with a host and no credentials"
        return None
    return s


def _list(v: Any, field: str, errors: dict):
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\n]", v)]
    if not isinstance(v, (list, tuple)):
        errors[field] = "must be a list"
        return None
    return list(v)


def _countries(v: Any, errors: dict) -> tuple[str, ...]:
    items = _list(v, "countries", errors)
    if items is None:
        return ()
    out = set()
    for x in items:
        if not isinstance(x, str):
            errors["countries"] = "must be 2-letter country codes"
            return ()
        x = x.strip().upper()
        if not x:
            continue
        if not _CC.fullmatch(x):
            errors["countries"] = f"invalid country code {x[:10]!r} (use ISO alpha-2, e.g. MX)"
            return ()
        out.add(x)
    return tuple(sorted(out))


def _methods(v: Any, errors: dict) -> tuple[str, ...]:
    items = _list(v, "payment_methods", errors)
    if items is None:
        return ()
    out: list[str] = []
    for x in items:
        if not isinstance(x, str):
            errors["payment_methods"] = "must be text items"
            return ()
        x = x.strip()
        if not x:
            continue
        if len(x) > 60 or re.search(r"[\x00-\x1f\x7f]", x):
            errors["payment_methods"] = "invalid payment method (max 60 chars, no control characters)"
            return ()
        if x not in out:
            out.append(x)
    if len(out) > 30:
        errors["payment_methods"] = "too many items (max 30)"
        return ()
    return tuple(out)


def build_program(program_id: Any, data: Mapping, *, active: Any, updated_by: Any, created_at: datetime,
                  updated_at: datetime) -> Program:
    """Validate and build. Raises ProgramValidationError with every field error at once."""
    errors: dict[str, str] = {}
    if not isinstance(program_id, str) or not _SLUG.fullmatch(program_id):
        errors["program_id"] = "use 2-64 chars: a-z, 0-9, '_' or '-', starting with a letter/digit"
    if not isinstance(active, bool):
        errors["active"] = "must be true or false"
    by = _text(updated_by, "updated_by", errors, max_len=64, required=True)
    for k in ("created_at", "updated_at"):
        t = created_at if k == "created_at" else updated_at
        if not isinstance(t, datetime) or t.tzinfo is None:
            errors[k] = "must be a timezone-aware datetime"
    unknown = sorted(set(data) - set(EDITABLE))
    if unknown:
        errors["_unknown"] = f"unknown fields: {unknown}"
    name = _text(data.get("name"), "name", errors, max_len=120)
    brand = _text(data.get("brand"), "brand", errors, max_len=120)
    countries = _countries(data.get("countries"), errors)
    cur = _text(data.get("currency"), "currency", errors, max_len=3)
    cur = cur.upper() if cur else None
    if cur and not _CUR.fullmatch(cur):
        errors["currency"] = "must be a 3-letter ISO code, e.g. MXN"
    url = _url(data.get("registration_url"), errors)
    promo = _text(data.get("promo_code"), "promo_code", errors, max_len=64)
    mdep, mwd = _money(data.get("min_deposit"), "min_deposit", errors), _money(data.get("min_withdrawal"), "min_withdrawal", errors)
    wtime = _text(data.get("withdrawal_time"), "withdrawal_time", errors, max_len=120)
    methods = _methods(data.get("payment_methods"), errors)
    terms = _text(data.get("terms"), "terms", errors, max_len=4000, multiline=True)
    support = _text(data.get("support_contact"), "support_contact", errors, max_len=255)
    notes = _text(data.get("notes"), "notes", errors, max_len=2000, multiline=True)
    if active is True:
        have = {"name": name, "brand": brand, "countries": countries, "currency": cur, "registration_url": url}
        for k in REQUIRED_WHEN_ACTIVE:
            if not have[k] and k not in errors:
                errors[k] = "required for an active program"
    if errors:
        raise ProgramValidationError(errors)
    return Program(program_id=program_id, name=name or "", brand=brand or "", countries=countries, currency=cur,
                   registration_url=url, promo_code=promo, min_deposit=mdep, min_withdrawal=mwd,
                   withdrawal_time=wtime, payment_methods=methods, terms=terms, support_contact=support,
                   notes=notes, active=active, created_at=created_at, updated_at=updated_at, updated_by=by or "")


def editable_view(p: Program) -> dict:
    """The editable fields of a Program as plain input data (for merge-on-update)."""
    d = p.to_dict()
    return {k: d[k] for k in EDITABLE}


__all__ = ["CONTRACT_VERSION", "EDITABLE", "REQUIRED_WHEN_ACTIVE", "Program", "ProgramValidationError",
           "build_program", "editable_view", "replace"]
