"""Pure validation for offer content. Collects field -> message errors (same style as copilot_programs)."""
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit

MAX_DESCRIPTION = 1000
MAX_IMAGES = 10
MAX_URL = 2048
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LINE_CTRL = re.compile(r"[\x00-\x1f\x7f]")           # single-line fields: newline / tab are rejected too
ACCOUNT_REF = re.compile(r"acct_[0-9a-f]{12}")


class ContentValidationError(ValueError):
    def __init__(self, errors):
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


_NUMERIC_HOST = re.compile(r"[0-9a-fx.]+", re.I)
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet", ".localdomain")


def public_host(host) -> bool:
    """Syntactic check only (NO DNS, NO network): rejects localhost, internal names and private/loopback/link-local/reserved IPs,
    including decimal/hex/octal-looking spellings."""
    if not isinstance(host, str) or not host:
        return False
    h = host.strip().lower().rstrip(".")
    if not h or h == "localhost" or h.endswith(_INTERNAL_SUFFIXES) or "." not in h and ":" not in h:
        return False                                    # single-label names (intranet hosts) are not public
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return not _NUMERIC_HOST.fullmatch(h)           # 2130706433 / 0x7f.1 style spellings are refused, normal names pass
    return ip.is_global and not ip.is_multicast


def valid_url(url) -> bool:
    """http(s) URL with a public host, no credentials, no whitespace/control characters."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        p = urlsplit(url)
        host, _ = p.hostname, p.port
    except ValueError:
        return False
    return p.scheme in ("http", "https") and p.username is None and p.password is None and public_host(host)


MAX_NAME = 120
MAX_PROMO = 64


def validate_offer(program, content):
    """-> None when the offer is usable, else the reason string 'INVALID'. Pure; never raises; no I/O.
    program: object with name, brand, registration_url, promo_code; content: {'description', 'images': [...]}."""
    try:
        name, brand = getattr(program, "name", None), getattr(program, "brand", None)
        for v in (name, brand):
            if not isinstance(v, str) or not v.strip() or len(v) > MAX_NAME or _LINE_CTRL.search(v):
                return "INVALID"
        if not valid_url(getattr(program, "registration_url", None)):
            return "INVALID"
        promo = getattr(program, "promo_code", None)
        if promo is not None and (not isinstance(promo, str) or len(promo) > MAX_PROMO or _LINE_CTRL.search(promo)):
            return "INVALID"
        content = content or {}
        desc = content.get("description")
        if desc is not None and (not isinstance(desc, str) or len(desc) > MAX_DESCRIPTION or _CTRL.search(desc)):
            return "INVALID"
        imgs = [i for i in (content.get("images") or []) if i.get("active", True)]
        if len(imgs) > MAX_IMAGES or any(not valid_url(i.get("url")) for i in imgs):
            return "INVALID"
        return None
    except Exception:
        return "INVALID"


def image_id_for(program_id: str, url: str) -> str:
    return "img_" + hashlib.sha1(f"{program_id}\n{url}".encode()).hexdigest()[:16]


def clean_content(description, images):
    """-> (description|None, [{'url','active'}]) in the given order, duplicates dropped. No fetching, no normalisation of hosts."""
    errors = {}
    desc = None
    if description is not None:
        if not isinstance(description, str):
            errors["description"] = "must be text"
        else:
            d = description.strip()
            if _CTRL.search(d):
                errors["description"] = "contains control characters"
            elif len(d) > MAX_DESCRIPTION:
                errors["description"] = f"at most {MAX_DESCRIPTION} characters"
            else:
                desc = d or None
    out, seen = [], set()
    if images is None:
        images = []
    if not isinstance(images, (list, tuple)):
        errors["images"] = "must be a list"
        images = []
    for i, it in enumerate(images):
        url, active = (it.get("url"), it.get("active", True)) if isinstance(it, dict) else (it, True)
        url = url.strip() if isinstance(url, str) else url
        if not valid_url(url):
            errors["images"] = f"image {i + 1}: invalid http(s) URL"
            break
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "active": bool(active)})
    if len(out) > MAX_IMAGES:
        errors["images"] = f"at most {MAX_IMAGES} images"
    if errors:
        raise ContentValidationError(errors)
    return desc, out
