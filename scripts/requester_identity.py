import re


def normalize_requester_identity(identity: str) -> str:
    """Normalize requester identity tokens for allowlist matching.

    Removes surrounding/internal whitespace and converts E.164-like phone
    numbers to ``tel:`` URIs.
    """
    value = identity.strip()
    without_whitespace = "".join(value.split())
    if re.fullmatch(r"\+\d[\d\s]*", value):
        return f"tel:{without_whitespace}"
    return without_whitespace
