"""Pure word-game logic (no Telegram imports) for /wordseek, /wordgrid, /wordchain.

Kept separate from bot/plugins/games.py so it can be unit-tested without the
bot runtime. All randomness goes through an injectable ``random.Random``.
"""
from __future__ import annotations

import random
import string
from typing import Optional

# Lexicon used for Wordseek targets and for Wordgrid's scoring dictionary.
# Wordgrid only scores lexicon words (there is no full dictionary bundled), so
# the grid generator always plants some of them.
LEXICON: tuple[str, ...] = tuple(sorted(set("""
ANIME MANGA SHONEN SEINEN ISEKAI WAIFU SENPAI KAWAII CHIBI MECHA NINJA SAMURAI DRAGON DEMON HERO
MAGIC QUEST GUILD RAID BOSS LOOT PIXEL GLITCH SWORD BLADE STORM CHAOS SHADOW FLAME FROST THUNDER
KNIGHT WIZARD ROGUE MAGE ARCHER ELF ORC GOBLIN TROLL SPIRIT GHOST CURSE SPELL RUNE RELIC TITAN
CASTLE TOWER DUNGEON FOREST OCEAN RIVER MOUNTAIN DESERT ISLAND STAR MOON SUN COMET GALAXY ORBIT
ROBOT CYBER LASER PLASMA QUARK ATOM ION BOND ACID BASE ESTER ALKENE ALKANE ARENE
GAME LEVEL SCORE COMBO CRIT BUFF NERF PATCH SERVER LAG PING NOOB PRO CLAN TEAM
CAT NEKO FOX WOLF BEAR LION TIGER EAGLE RAVEN SNAKE
RED BLUE GOLD IRON STONE STEEL JADE RUBY AMBER
RAIN SNOW WIND FIRE EARTH WATER LIGHT DARK NIGHT DAWN
TEA COFFEE RAMEN SUSHI MOCHI
""".split())))

_VOWELS = "AEIOU"
_CONSONANTS = "BCDFGHJKLMNPRSTVWY"
_DIRS8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


# ── Wordseek ────────────────────────────────────────────────────────
def make_wordseek_grid(word: str, size: int = 8, rng: Optional[random.Random] = None) -> list[list[str]]:
    """Hide ``word`` (forward only: right, down, or down-right) in a size×size grid."""
    rng = rng or random
    word = word.upper()
    if not word.isalpha() or len(word) > size:
        raise ValueError("word must be alphabetic and fit in the grid")
    grid = [[rng.choice(string.ascii_uppercase) for _ in range(size)] for _ in range(size)]
    dr, dc = rng.choice([(0, 1), (1, 0), (1, 1)])
    max_r = size - 1 - dr * (len(word) - 1)
    max_c = size - 1 - dc * (len(word) - 1)
    r, c = rng.randint(0, max_r), rng.randint(0, max_c)
    for i, ch in enumerate(word):
        grid[r + dr * i][c + dc * i] = ch
    return grid


def grid_contains_straight(grid: list[list[str]], word: str) -> bool:
    """True if word appears in a straight line (right/down/down-right) — test helper."""
    n = len(grid)
    for r in range(n):
        for c in range(n):
            for dr, dc in ((0, 1), (1, 0), (1, 1)):
                er, ec = r + dr * (len(word) - 1), c + dc * (len(word) - 1)
                if er < n and ec < n and all(grid[r + dr * i][c + dc * i] == ch for i, ch in enumerate(word)):
                    return True
    return False


# ── Wordgrid (Boggle-style) ─────────────────────────────────────────
def find_path(grid: list[list[str]], word: str) -> bool:
    """True if ``word`` can be traced through adjacent tiles (8-neighbour, no tile reused)."""
    word = word.upper()
    n = len(grid)
    if not word:
        return False

    def dfs(r: int, c: int, i: int, used: frozenset) -> bool:
        if grid[r][c] != word[i]:
            return False
        if i == len(word) - 1:
            return True
        used = used | {(r, c)}
        for dr, dc in _DIRS8:
            nr, nc = r + dr, c + dc
            if 0 <= nr < n and 0 <= nc < n and (nr, nc) not in used and dfs(nr, nc, i + 1, used):
                return True
        return False

    return any(dfs(r, c, 0, frozenset()) for r in range(n) for c in range(n))


def _plant(grid: list[list[Optional[str]]], word: str, rng: random.Random) -> bool:
    n = len(grid)

    def walk(r: int, c: int, i: int, used: list) -> bool:
        cell = grid[r][c]
        if cell is not None and cell != word[i]:
            return False
        if i == len(word) - 1:
            grid[r][c] = word[i]
            return True
        prev = grid[r][c]
        grid[r][c] = word[i]
        used.append((r, c))
        dirs = _DIRS8[:]
        rng.shuffle(dirs)
        for dr, dc in dirs:
            nr, nc = r + dr, c + dc
            if 0 <= nr < n and 0 <= nc < n and (nr, nc) not in used and walk(nr, nc, i + 1, used):
                return True
        used.pop()
        grid[r][c] = prev
        return False

    cells = [(r, c) for r in range(n) for c in range(n)]
    rng.shuffle(cells)
    return any(walk(r, c, 0, []) for r, c in cells)


def make_wordgrid(rng: Optional[random.Random] = None, size: int = 4, plant: int = 4) -> tuple[list[list[str]], list[str]]:
    """Return (grid, planted_words). Planted words are guaranteed traceable."""
    rng = rng or random.Random()
    pool = [w for w in LEXICON if 3 <= len(w) <= size + 1]
    for _ in range(50):
        grid: list[list[Optional[str]]] = [[None] * size for _ in range(size)]
        planted: list[str] = []
        for w in rng.sample(pool, min(len(pool), plant * 3)):
            if len(planted) >= plant:
                break
            backup = [row[:] for row in grid]
            if _plant(grid, w, rng):
                planted.append(w)
            else:
                grid = backup
        if len(planted) >= 2:
            break
    for r in range(size):
        for c in range(size):
            if grid[r][c] is None:
                grid[r][c] = rng.choice(_VOWELS) if rng.random() < 0.35 else rng.choice(_CONSONANTS)
    return grid, planted  # type: ignore[return-value]


def score_word(word: str) -> int:
    return max(0, len(word) - 2)


def judge_grid_word(grid: list[list[str]], found: set[str], word: str) -> tuple[bool, str]:
    """Validate a Wordgrid guess. Returns (accepted, reason_if_rejected)."""
    word = word.upper()
    if len(word) < 3:
        return False, "too short (3+ letters)"
    if word in found:
        return False, "already found"
    if word not in LEXICON:
        return False, "not in this game's word list"
    if not find_path(grid, word):
        return False, "can't be traced from adjacent tiles"
    return True, ""


# ── Wordchain ───────────────────────────────────────────────────────
def judge_chain_word(last: str, used: set[str], last_user: Optional[int], user_id: int, word: str) -> tuple[bool, str]:
    """Validate a Wordchain move. There is no dictionary: alphabetic words only."""
    word = word.upper()
    if not word.isalpha() or not word.isascii():
        return False, "letters only"
    if len(word) < 3:
        return False, "at least 3 letters"
    if word[0] != last[-1]:
        return False, f"must start with {last[-1]}"
    if word in used:
        return False, "already used"
    if last_user is not None and last_user == user_id:
        return False, "let someone else go"
    return True, ""
