"""Username generation & availability — pure candidate generation + bounded checks.

``generate_candidates`` is a PURE function (no network): it normalises a base
string to Telegram's username rules and derives variations. ``rng`` is injected
so tests are deterministic — never reaches for the global ``random``.

``find_available`` checks candidates SEQUENTIALLY through ``client.check_username``
and stops as soon as ``limit`` free names are found (or candidates run out): a
single sequential pass, never a parallel storm — the read anti-flood discipline.
``find_available_marked`` is the same pass but also returns the unchecked tail
(candidates past the limit), so the UI can mark them ``?`` next to the ``✓`` free.

Telegram username rules (the only authority here): 5–32 chars, ``[a-z0-9_]``
only, must start with a letter, must not end with ``_``, no ``__`` run.
"""

from __future__ import annotations

import random
import re
from collections.abc import Awaitable
from datetime import datetime, timezone
from typing import Protocol

USERNAME_MIN_LEN = 5
USERNAME_MAX_LEN = 32

_VALID_RE = re.compile(r"^[a-z][a-z0-9_]{3,30}[a-z0-9]$")

# Minimal Cyrillic→Latin transliteration: enough to turn a Russian name into a
# valid ASCII base. Unmapped non-ascii chars are simply dropped (documented).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# moderate leet replacements — applied only when the result stays valid
_LEET = {"o": "0", "i": "1", "e": "3", "a": "4", "s": "5"}


def is_valid_username(name: str) -> bool:
    """True iff ``name`` satisfies Telegram's username rules.

    5–32 chars, lowercase ``[a-z0-9_]`` only, starts with a letter, does not end
    with ``_``, and has no ``__`` run.
    """
    if not (USERNAME_MIN_LEN <= len(name) <= USERNAME_MAX_LEN):
        return False
    if "__" in name:
        return False
    return _VALID_RE.match(name) is not None


def _transliterate(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in text)


def _normalize_base(base: str) -> str:
    """Reduce an arbitrary string to a clean lowercase ``[a-z0-9_]`` stem.

    Cyrillic is transliterated; any remaining non-ascii is dropped. Leading
    digits/underscores are stripped (a username must start with a letter), and
    a trailing underscore is trimmed.
    """
    text = _transliterate(base.lower())
    # keep only [a-z0-9_], collapse repeated underscores
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text)
    text = text.lstrip("0123456789_")  # must start with a letter
    text = text.strip("_")
    return text


def _trim(name: str) -> str:
    """Clamp to <=32 chars and strip any trailing underscore left by trimming."""
    return name[:USERNAME_MAX_LEN].rstrip("_")


def _pad(name: str, rng: random.Random) -> str:
    """Pad a too-short stem with random digits until it reaches the min length."""
    while len(name) < USERNAME_MIN_LEN:
        name += str(rng.randint(0, 9))
    return name


def generate_candidates(base: str, *, count: int = 20, rng: random.Random | None = None) -> list[str]:
    """Derive up to ``count`` valid Telegram username candidates from ``base``.

    PURE and deterministic for a fixed ``rng``: normalises ``base`` (translit +
    ascii-filter), then layers suffixes (digits, the current year),
    word-underscore joins and moderate leet swaps. Every returned candidate
    passes :func:`is_valid_username`; the list is deduped and order-stable.
    """
    if rng is None:
        rng = random.Random()
    stem = _normalize_base(base)
    if not stem:
        stem = "user"
    stem = _pad(stem, rng)

    year = datetime.now(timezone.utc).year
    seen: set[str] = set()
    out: list[str] = []

    def add(name: str) -> None:
        name = _trim(name)
        if is_valid_username(name) and name not in seen:
            seen.add(name)
            out.append(name)

    # 1) the bare stem (and an underscore-joined variant if it has digits inside)
    add(stem)

    # 2) numeric / year suffixes
    for suffix in (str(year), str(year % 100), "01", "1", "_01"):
        add(stem + suffix)
        if len(out) >= count:
            return out[:count]

    # 3) a leet variant of the stem, plus leet + suffix
    leet = "".join(_LEET.get(ch, ch) for ch in stem)
    add(leet)
    add(leet + str(year % 100))

    # 4) random fillers until we reach count (or give up after a bounded budget)
    attempts = 0
    while len(out) < count and attempts < count * 20:
        attempts += 1
        roll = rng.randint(0, 3)
        if roll == 0:
            add(stem + str(rng.randint(0, 9999)))
        elif roll == 1:
            add(stem + "_" + str(rng.randint(1, 99)))
        elif roll == 2:
            add("".join(_LEET.get(ch, ch) if rng.random() < 0.4 else ch for ch in stem))
        else:
            add(stem + rng.choice("abcdefghijklmnopqrstuvwxyz"))

    return out[:count]


class _UsernameChecker(Protocol):
    def check_username(self, username: str) -> Awaitable[bool]: ...


async def find_available(
    client: _UsernameChecker,
    base: str,
    *,
    limit: int = 10,
    count: int = 20,
    rng: random.Random | None = None,
) -> list[str]:
    """Return up to ``limit`` available usernames derived from ``base``.

    Generates ``count`` candidates, then checks them SEQUENTIALLY through
    ``client.check_username`` (one network call at a time — flood discipline),
    collecting the free ones in generation order. Stops as soon as ``limit``
    free names are found, so at most ``count`` network checks ever happen.
    """
    free, _ = await find_available_marked(client, base, limit=limit, count=count, rng=rng)
    return free


async def find_available_marked(
    client: _UsernameChecker,
    base: str,
    *,
    limit: int = 10,
    count: int = 20,
    rng: random.Random | None = None,
) -> tuple[list[str], list[str]]:
    """Like :func:`find_available`, but also report the *unchecked* candidates.

    Returns ``(free, unchecked)`` where ``free`` are verified-available names (at
    most ``limit``, same sequential flood-disciplined check as ``find_available``)
    and ``unchecked`` are the remaining generated candidates that were never sent
    to ``check_username`` because the ``limit`` was already reached — the UI marks
    these ``?`` (generated but availability unknown). When the whole pool is
    exhausted by checks, ``unchecked`` is empty.
    """
    candidates = generate_candidates(base, count=count, rng=rng)
    free: list[str] = []
    for i, name in enumerate(candidates):
        if len(free) >= limit:
            # limit reached → stop checking; everything from here on is unknown
            return free, candidates[i:]
        if await client.check_username(name):
            free.append(name)
    return free, []
