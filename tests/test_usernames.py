"""Tests for core.usernames — candidate generation (pure, rng-injected) + find_available."""

from __future__ import annotations

import random

import pytest

from tg_messenger.core.usernames import (
    find_available,
    find_available_marked,
    generate_candidates,
    is_valid_username,
)

# --- is_valid_username (Telegram rules helper) ---


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ann_smith", True),
        ("abcde", True),  # exactly 5
        ("a" * 32, True),  # exactly 32
        ("ann", False),  # too short (<5)
        ("a" * 33, False),  # too long (>32)
        ("1annsmith", False),  # must start with a letter
        ("_annsmith", False),  # must start with a letter
        ("annsmith_", False),  # must not end with underscore
        ("ann__smith", False),  # no double underscore
        ("Ann_Smith", False),  # only lowercase a-z allowed
        ("ann-smith", False),  # no dashes
        ("ann smith", False),  # no spaces
        ("аннsmith", False),  # no cyrillic
    ],
)
def test_is_valid_username(name, expected):
    assert is_valid_username(name) is expected


# --- generate_candidates ---


def test_all_candidates_valid():
    rng = random.Random(0)
    cands = generate_candidates("Ann", count=20, rng=rng)
    assert cands, "expected at least one candidate"
    for c in cands:
        assert is_valid_username(c), f"{c!r} is not a valid Telegram username"


def test_short_base_padded_to_min_length():
    rng = random.Random(0)
    cands = generate_candidates("ab", count=20, rng=rng)
    assert cands
    for c in cands:
        assert len(c) >= 5
        assert is_valid_username(c)


def test_cyrillic_base_handled():
    rng = random.Random(0)
    cands = generate_candidates("Анна", count=20, rng=rng)
    assert cands, "cyrillic base must still yield candidates"
    for c in cands:
        assert is_valid_username(c)


def test_first_char_always_letter():
    rng = random.Random(0)
    # a digit-heavy base must never produce a username starting with a digit
    cands = generate_candidates("123go", count=20, rng=rng)
    assert cands
    for c in cands:
        assert c[0].isalpha()
        assert is_valid_username(c)


def test_deterministic_same_rng_same_result():
    a = generate_candidates("Ann", count=20, rng=random.Random(42))
    b = generate_candidates("Ann", count=20, rng=random.Random(42))
    assert a == b


def test_no_duplicates():
    cands = generate_candidates("Ann", count=20, rng=random.Random(0))
    assert len(cands) == len(set(cands))


def test_count_is_respected():
    cands = generate_candidates("Ann", count=5, rng=random.Random(0))
    assert len(cands) <= 5


def test_empty_base_still_valid():
    # base that normalises to nothing (only non-ascii / punctuation)
    cands = generate_candidates("!!!", count=10, rng=random.Random(0))
    for c in cands:
        assert is_valid_username(c)


# --- find_available ---


class _FakeUsernameClient:
    def __init__(self, occupied):
        self.occupied = set(occupied)
        self.checked: list[str] = []

    async def check_username(self, username):
        self.checked.append(username)
        return username not in self.occupied


async def test_find_available_returns_only_free():
    cands = generate_candidates("Ann", count=20, rng=random.Random(0))
    occupied = set(cands[:3])  # first three taken
    client = _FakeUsernameClient(occupied)
    free = await find_available(client, "Ann", limit=10, count=20, rng=random.Random(0))
    assert free, "expected some free names"
    assert all(name not in occupied for name in free)
    # order preserved (subsequence of generation order)
    gen_order = [c for c in cands if c not in occupied]
    assert free == gen_order[: len(free)]


async def test_find_available_stops_at_limit():
    # nothing occupied → should stop after `limit` checks, not check all `count`
    client = _FakeUsernameClient(occupied=set())
    free = await find_available(client, "Ann", limit=3, count=20, rng=random.Random(0))
    assert len(free) == 3
    assert len(client.checked) == 3  # stopped early — no flood storm


async def test_find_available_bounded_by_count():
    # everything occupied → checks at most `count` candidates, returns nothing
    cands = generate_candidates("Ann", count=20, rng=random.Random(0))
    client = _FakeUsernameClient(occupied=set(cands))
    free = await find_available(client, "Ann", limit=10, count=20, rng=random.Random(0))
    assert free == []
    assert len(client.checked) <= 20


# --- find_available_marked (verified-free + unchecked tail) ---


async def test_find_available_marked_returns_free_and_unchecked():
    # nothing occupied: stops at `limit` free, the rest are unchecked (not a flood storm)
    cands = generate_candidates("Ann", count=20, rng=random.Random(0))
    client = _FakeUsernameClient(occupied=set())
    free, unchecked = await find_available_marked(
        client, "Ann", limit=3, count=20, rng=random.Random(0)
    )
    assert free == cands[:3]
    # exactly `limit` network checks, then we stop — no more
    assert len(client.checked) == 3
    # everything generated past the verified ones is reported as unchecked
    assert unchecked == cands[3:]
    # the two lists partition the checked-or-generated candidates, no overlap
    assert set(free).isdisjoint(unchecked)


async def test_find_available_marked_no_unchecked_when_all_checked():
    # everything occupied → all `count` candidates checked, none free, none unchecked
    cands = generate_candidates("Ann", count=20, rng=random.Random(0))
    client = _FakeUsernameClient(occupied=set(cands))
    free, unchecked = await find_available_marked(
        client, "Ann", limit=10, count=20, rng=random.Random(0)
    )
    assert free == []
    assert unchecked == []  # the whole pool was exhausted by checks
    assert len(client.checked) == len(cands)


async def test_find_available_marked_unchecked_only_past_limit():
    # first three occupied, plenty free after: still stop at `limit` free names,
    # so the unchecked tail starts only after the last verified-free candidate
    cands = generate_candidates("Ann", count=20, rng=random.Random(0))
    occupied = set(cands[:3])
    client = _FakeUsernameClient(occupied)
    free, unchecked = await find_available_marked(
        client, "Ann", limit=2, count=20, rng=random.Random(0)
    )
    assert len(free) == 2
    assert all(name not in occupied for name in free)
    # checked = the 3 occupied (rejected) + 2 free = 5; the rest are unchecked
    assert len(client.checked) == 5
    assert unchecked == cands[5:]
