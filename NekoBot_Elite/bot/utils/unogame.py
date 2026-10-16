"""Pure UNO rules engine (no Telegram imports). Used by /uno in bot/plugins/games.py.

Cards are (color, value) tuples. Colors: R G B Y (wilds carry color None in hand).
Values: "0"-"9", "SKIP", "REV", "+2", "WILD", "+4".
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

COLORS = ("R", "G", "B", "Y")
COLOR_EMOJI = {"R": "🔴", "G": "🟢", "B": "🔵", "Y": "🟡"}
_COLOR_ALIASES = {
    "r": "R", "red": "R", "🔴": "R", "g": "G", "green": "G", "🟢": "G",
    "b": "B", "blue": "B", "🔵": "B", "y": "Y", "yellow": "Y", "🟡": "Y",
}
_VALUE_ALIASES = {
    "skip": "SKIP", "s": "SKIP", "rev": "REV", "reverse": "REV", "r": "REV",
    "+2": "+2", "d2": "+2", "draw2": "+2", "wild": "WILD", "w": "WILD",
    "+4": "+4", "d4": "+4", "draw4": "+4", "wild4": "+4",
}
MAX_PLAYERS = 6
HAND_SIZE = 7

Card = tuple[Optional[str], str]


def card_str(card: Card) -> str:
    color, value = card
    if color is None:
        return "🌈" + ("Wild" if value == "WILD" else "+4")
    label = {"SKIP": "Skip", "REV": "Reverse"}.get(value, value)
    return f"{COLOR_EMOJI[color]}{label}"


def build_deck(rng: random.Random) -> list[Card]:
    deck: list[Card] = []
    for c in COLORS:
        deck.append((c, "0"))
        for v in [str(i) for i in range(1, 10)] + ["SKIP", "REV", "+2"]:
            deck += [(c, v), (c, v)]
    deck += [(None, "WILD")] * 4 + [(None, "+4")] * 4
    rng.shuffle(deck)
    return deck


def parse_card(tokens: list[str]) -> tuple[Optional[Card], Optional[str]]:
    """Parse user text like ['r','5'], ['🔴5'], ['red','skip'], ['wild','blue'], ['+4','g'].

    Returns (card, chosen_color). For a wild the card color is None and
    chosen_color is the declared color (may be None if omitted).
    """
    text = " ".join(tokens).strip().lower()
    if not text:
        return None, None
    parts = text.replace("+", " +").split()
    # normalise an attached emoji/letter color prefix: "🔴5", "r5"
    flat: list[str] = []
    for p in parts:
        if p[0] in "🔴🟢🔵🟡" and len(p) > 1:
            flat += [p[0], p[1:]]
        elif len(p) >= 2 and p[0] in "rgby" and (p[1:].isdigit() or p[1:] in ("skip", "rev", "reverse", "+2")):
            flat += [p[0], p[1:]]
        else:
            flat.append(p)
    if not flat:
        return None, None
    if flat[0] in ("wild", "w", "+4", "d4", "draw4", "wild4"):
        value = _VALUE_ALIASES[flat[0]]
        color = _COLOR_ALIASES.get(flat[1]) if len(flat) > 1 else None
        return (None, value), color
    color = _COLOR_ALIASES.get(flat[0])
    if color is None or len(flat) < 2:
        return None, None
    v = flat[1]
    value = v if v.isdigit() and len(v) == 1 else _VALUE_ALIASES.get(v)
    if value is None or value in ("WILD", "+4"):
        return None, None
    return (color, value), None


@dataclass
class Player:
    id: int
    name: str
    hand: list[Card] = field(default_factory=list)


@dataclass
class PlayResult:
    ok: bool
    message: str = ""
    winner: Optional[Player] = None


class UnoGame:
    def __init__(self, host_id: int, host_name: str, rng: Optional[random.Random] = None):
        self.rng = rng or random.Random()
        self.players: list[Player] = [Player(host_id, host_name)]
        self.deck: list[Card] = []
        self.discard: list[Card] = []
        self.color: Optional[str] = None
        self.turn = 0
        self.direction = 1
        self.started = False

    # lobby
    def add_player(self, uid: int, name: str) -> tuple[bool, str]:
        if self.started:
            return False, "Game already started."
        if any(p.id == uid for p in self.players):
            return False, "Already in the lobby."
        if len(self.players) >= MAX_PLAYERS:
            return False, f"Lobby full ({MAX_PLAYERS})."
        self.players.append(Player(uid, name))
        return True, ""

    def start(self) -> tuple[bool, str]:
        if self.started:
            return False, "Already started."
        if len(self.players) < 2:
            return False, "Need at least 2 players."
        self.deck = build_deck(self.rng)
        for p in self.players:
            p.hand = [self.deck.pop() for _ in range(HAND_SIZE)]
        # first upcard must be a plain number card
        while True:
            card = self.deck.pop()
            if card[0] is not None and card[1].isdigit():
                break
            self.deck.insert(0, card)
        self.discard = [card]
        self.color = card[0]
        self.started = True
        self.turn = 0
        return True, ""

    # helpers
    @property
    def current(self) -> Player:
        return self.players[self.turn]

    @property
    def top(self) -> Card:
        return self.discard[-1]

    def player(self, uid: int) -> Optional[Player]:
        return next((p for p in self.players if p.id == uid), None)

    def _advance(self, steps: int = 1) -> None:
        self.turn = (self.turn + self.direction * steps) % len(self.players)

    def _draw(self, p: Player, n: int) -> int:
        drawn = 0
        for _ in range(n):
            if not self.deck:
                if len(self.discard) <= 1:
                    break
                top = self.discard.pop()
                self.deck = self.discard
                self.rng.shuffle(self.deck)
                self.discard = [top]
            p.hand.append(self.deck.pop())
            drawn += 1
        return drawn

    def playable(self, card: Card) -> bool:
        if card[0] is None:
            return True
        return card[0] == self.color or card[1] == self.top[1]

    # actions
    def draw_turn(self, uid: int) -> PlayResult:
        if not self.started:
            return PlayResult(False, "Not started.")
        p = self.current
        if p.id != uid:
            return PlayResult(False, f"Not your turn — {p.name} is up.")
        n = self._draw(p, 1)
        self._advance()
        return PlayResult(True, f"{p.name} drew a card." if n else f"{p.name} couldn't draw (deck empty).")

    def play(self, uid: int, tokens: list[str]) -> PlayResult:
        if not self.started:
            return PlayResult(False, "Not started.")
        p = self.current
        if p.id != uid:
            return PlayResult(False, f"Not your turn — {p.name} is up.")
        card, chosen = parse_card(tokens)
        if card is None:
            return PlayResult(False, "Couldn't read that card. Examples: /uno play r5 · y skip · wild blue · +4 g")
        if card not in p.hand:
            return PlayResult(False, "You don't have that card. Use /uno hand.")
        if card[0] is None and chosen is None:
            return PlayResult(False, "Pick a color: e.g. /uno play wild blue")
        if not self.playable(card):
            return PlayResult(False, f"Doesn't match {card_str(self.top)} ({COLOR_EMOJI.get(self.color, '')}).")
        p.hand.remove(card)
        self.discard.append(card)
        self.color = chosen if card[0] is None else card[0]
        notes = [f"{p.name} played {card_str(card)}"]
        if not p.hand:
            return PlayResult(True, notes[0] + f"\n🏆 {p.name} wins UNO!", winner=p)
        value = card[1]
        if value == "REV":
            self.direction *= -1
            notes.append("Direction reversed.")
            self._advance(2 if len(self.players) == 2 else 1)
        elif value == "SKIP":
            self._advance()
            notes.append(f"{self.current.name} is skipped.")
            self._advance()
        elif value in ("+2", "+4"):
            self._advance()
            victim = self.current
            k = self._draw(victim, 2 if value == "+2" else 4)
            notes.append(f"{victim.name} draws {k} and is skipped.")
            self._advance()
        else:
            self._advance()
        if card[0] is None:
            notes.append(f"Color: {COLOR_EMOJI[self.color]}")
        if len(p.hand) == 1:
            notes.append(f"⚠️ {p.name} has UNO!")
        notes.append(f"Top: {card_str(card)} · Next: {self.current.name}")
        return PlayResult(True, "\n".join(notes))
