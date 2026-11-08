"""Offline regex scrubber: replace sensitive values with typed placeholders.

    clean, counts = scrub(text)
    # counts == {"EMAIL": 1, "IP": 0, "CARD": 0, "API_KEY": 0, "TOKEN": 2, "PHONE": 0}

Order matters: tokens/keys first (they can contain digit runs), then email, card, IP, phone.
Placeholders contain no digits or '@', so later patterns never re-match them.
"""
import re

TYPES = ("EMAIL", "IP", "CARD", "API_KEY", "TOKEN", "PHONE")

# --- tokens -----------------------------------------------------------------------------
_BEARER = re.compile(r"(?i)\b(bearer\s+)(?!\[TOKEN\])[A-Za-z0-9\-._~+/]{8,}=*")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")

# --- API keys (Stripe-style, AWS access key id, GitHub, Slack) ----------------------------
_API_KEY = re.compile(
    r"\b(?:(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,})\b")

# --- email (written and spoken, e.g. from a voice transcript) ----------------------------
_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_SPOKEN_EMAIL = re.compile(
    r"(?i)\b[a-z0-9._-]+(?:\s+dot\s+[a-z0-9_-]+)*\s+at\s+[a-z0-9-]+(?:\s+dot\s+[a-z0-9-]+)*"
    r"\s+dot\s+(?:com|net|org|io|co|edu|gov|dev|ai|uk|de)\b")

# --- cards: full PANs (Luhn-checked) and masked/partial PANs ------------------------------
_M = r"[0-9*Xx]"
_CARD_GROUPED = re.compile(rf"(?<![\w*])(?:{_M}{{4}}[ -]){{2,3}}{_M}{{1,7}}(?![\w*])")
_CARD_RUN = re.compile(rf"(?<![\w*]){_M}{{12,19}}(?![\w*])")


def _luhn(digits):
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


def _is_card(s):
    digits = re.sub(r"\D", "", s)
    masked = sum(c in "*Xx" for c in s)
    if len(digits) < 4:
        return False
    if masked >= 4:  # partial / masked PAN, e.g. 4111 **** **** 1234
        return len(digits) + masked >= 12
    return 13 <= len(digits) <= 19 and digits[0] in "3456" and _luhn(digits)


# --- IPv4 -------------------------------------------------------------------------------
_OCT = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IP = re.compile(rf"(?<![\w.]){_OCT}(?:\.{_OCT}){{3}}(?!\w|\.\d)")

# --- phone numbers (NANP + international with leading '+') -------------------------------
_PHONE = re.compile(
    r"(?<![\w+])(?:"
    r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}"
    r"|\+\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){2,5}"
    r")(?!\w|\.\d)")


def _phone_ok(s):
    return 10 <= len(re.sub(r"\D", "", s)) <= 15 or (s.startswith("+") and len(re.sub(r"\D", "", s)) >= 8)


def scrub(text):
    """Return (clean_text, counts) where counts maps each type in TYPES to #replacements."""
    counts = dict.fromkeys(TYPES, 0)
    if not text:
        return text, counts

    def sub(pattern, kind, s, check=None, keep_group=None):
        def repl(m):
            if check and not check(m.group(0)):
                return m.group(0)
            counts[kind] += 1
            prefix = m.group(keep_group) if keep_group else ""
            return f"{prefix}[{kind}]"
        return pattern.sub(repl, s)

    text = sub(_BEARER, "TOKEN", text, keep_group=1)
    text = sub(_JWT, "TOKEN", text)
    text = sub(_API_KEY, "API_KEY", text)
    text = sub(_EMAIL, "EMAIL", text)
    text = sub(_SPOKEN_EMAIL, "EMAIL", text)
    text = sub(_CARD_GROUPED, "CARD", text, check=_is_card)
    text = sub(_CARD_RUN, "CARD", text, check=_is_card)
    text = sub(_IP, "IP", text)
    text = sub(_PHONE, "PHONE", text, check=_phone_ok)
    return text, counts


def scrub_obj(obj, counts=None):
    """Recursively scrub every string inside dicts/lists. Returns (clean_obj, counts)."""
    counts = counts if counts is not None else dict.fromkeys(TYPES, 0)
    if isinstance(obj, str):
        clean, c = scrub(obj)
        for k, v in c.items():
            counts[k] += v
        return clean, counts
    if isinstance(obj, dict):
        return {k: scrub_obj(v, counts)[0] for k, v in obj.items()}, counts
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v, counts)[0] for v in obj], counts
    return obj, counts
