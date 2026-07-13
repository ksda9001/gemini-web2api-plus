"""Cookie-file parsing shared by the direct and Gemini Web session backends."""
import json
import os
import time


def _flat_cookie_pairs(value: str) -> dict:
    pairs = {}
    for part in (value or "").split(";"):
        if "=" not in part:
            continue
        name, cookie_value = part.strip().split("=", 1)
        if name:
            pairs[name] = cookie_value
    return pairs


def _expiry_timestamp(cookie: dict):
    value = cookie.get("expirationDate", cookie.get("expires", cookie.get("expiry")))
    try:
        expiry = float(value)
    except (TypeError, ValueError):
        return None
    if expiry <= 0:
        return None
    # Playwright/Chrome variants occasionally serialize milliseconds.
    return expiry / 1000 if expiry > 100000000000 else expiry


def _matches_gemini_host(cookie: dict) -> bool:
    domain = str(cookie.get("domain", "")).strip().lower().lstrip(".")
    if not domain:
        return True
    if bool(cookie.get("hostOnly", False)):
        return domain == "gemini.google.com"
    return domain in ("google.com", "gemini.google.com")


def cookie_pairs_from_content(content: str, now: float = None) -> tuple:
    """Return browser-valid Gemini cookie pairs and the next known expiry.

    Supports the project's compact ``name=value`` format as well as cookie
    exports containing a list of browser-cookie objects. The latter retains
    expiry metadata, allowing expired short-lived security cookies to be
    omitted just as a browser would omit them.
    """
    content = (content or "").strip()
    if not content:
        return {}, None
    now = time.time() if now is None else now

    if not content.startswith(("{", "[")):
        return _flat_cookie_pairs(content), None

    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return _flat_cookie_pairs(content), None

    if isinstance(payload, dict) and isinstance(payload.get("cookie"), str):
        pairs = _flat_cookie_pairs(payload["cookie"])
        sapisid = payload.get("sapisid")
        if isinstance(sapisid, str) and sapisid and not pairs.get("SAPISID"):
            pairs["SAPISID"] = sapisid
        return pairs, None

    if isinstance(payload, dict):
        records = payload.get("cookies")
    else:
        records = payload
    if not isinstance(records, list):
        return {}, None

    pairs = {}
    next_expiry = None
    for cookie in records:
        if not isinstance(cookie, dict) or not _matches_gemini_host(cookie):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            continue
        expiry = _expiry_timestamp(cookie)
        if expiry is not None and expiry <= now:
            continue
        if expiry is not None and (next_expiry is None or expiry < next_expiry):
            next_expiry = expiry
        pairs[name] = value
    return pairs, next_expiry


def load_cookie_pairs(path: str, now: float = None) -> tuple:
    """Load a cookie file without exposing its values to callers' logs."""
    if not path or not os.path.exists(path):
        return {}, None
    try:
        with open(path, "r") as file:
            return cookie_pairs_from_content(file.read(), now)
    except OSError:
        return {}, None


def cookie_header(pairs: dict) -> str:
    return "; ".join(f"{name}={value}" for name, value in (pairs or {}).items())
