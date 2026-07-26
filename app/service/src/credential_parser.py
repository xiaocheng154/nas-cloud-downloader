from __future__ import annotations

import re


_COOKIE_PREFIX = re.compile(r"^\s*cookie\s*:\s*", re.IGNORECASE)


def parse_cookie_pairs(raw: str) -> list[tuple[str, str]]:
    """Parse a copied Cookie header while preserving the first valid value."""
    text = _COOKIE_PREFIX.sub("", str(raw or ""), count=1)
    text = text.replace("\r\n", ";").replace("\r", ";").replace("\n", ";")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in text.split(";"):
        item = part.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value or name in seen:
            continue
        seen.add(name)
        pairs.append((name, value))
    return pairs


def normalize_cookie(raw: str) -> str:
    return "; ".join(f"{name}={value}" for name, value in parse_cookie_pairs(raw))


def extract_baidu_credentials(raw: str) -> dict[str, str]:
    values = {name.upper(): value for name, value in parse_cookie_pairs(raw)}
    return {
        "bduss": values.get("BDUSS", ""),
        "stoken": values.get("STOKEN", ""),
    }
