"""Dependency-free tests for the pure game engines (bot/utils/wordgames.py, unogame.py)."""
from __future__ import annotations

import importlib.util
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    # Load by path so this file runs without the bot's runtime dependencies.
    spec = importlib.util.spec_from_file_location(name, ROOT / "bot" / "utils" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


wg = _load("wordgames")
uno = _load("unogame")


class TestWordseek(unittest.TestCase):
    def test_hidden_word_always_present(self):
        for seed in range(200):
            rng = random.Random(seed)
            word = rng.choice([w for w in wg.LEXICON if len(w) <= 8])
            grid = wg.make_wordseek_grid(word, 8, rng)
            self.assertTrue(wg.grid_contains_straight(grid, word), (seed, word))

    def test_rejects_bad_words(self):
        with self.assertRaises(ValueError):
            wg.make_wordseek_grid("TOOLONGWORD", 8)
        with self.assertRaises(ValueError):
            wg.make_wordseek_grid("AB1", 8)


class TestWordgrid(unittest.TestCase):
    def test_planted_words_are_traceable_and_scored(self):
        for seed in range(150):
            grid, planted = wg.make_wordgrid(random.Random(seed))
            self.assertGreaterEqual(len(planted), 2)
            for w in planted:
                ok, why = wg.judge_grid_word(grid, set(), w)
                self.assertTrue(ok, (seed, w, why))

    def test_rejections(self):
        grid = [list("ABCD"), list("EFGH"), list("IJKL"), list("MNOP")]
        self.assertFalse(wg.judge_grid_word(grid, set(), "AB")[0])
        self.assertEqual(wg.judge_grid_word(grid, {"CAT"}, "cat")[1], "already found")
        self.assertFalse(wg.judge_grid_word(grid, set(), "ZZZZ")[0])

    def test_tile_cannot_be_reused(self):
        self.assertFalse(wg.find_path([list("AB"), list("CD")], "ABA"))
        self.assertTrue(wg.find_path([list("AB"), list("CD")], "ABD"))


class TestWordchain(unittest.TestCase):
    def test_rules(self):
        j = wg.judge_chain_word
        self.assertTrue(j("ANIME", {"ANIME"}, None, 1, "eagle")[0])
        self.assertFalse(j("ANIME", {"ANIME"}, None, 1, "tiger")[0])   # wrong letter
        self.assertFalse(j("ANIME", {"ANIME", "EAGLE"}, 2, 1, "eagle")[0])  # repeat
        self.assertFalse(j("ANIME", {"ANIME"}, 1, 1, "eagle")[0])       # same player twice
        self.assertFalse(j("ANIME", {"ANIME"}, None, 1, "e1")[0])
        self.assertFalse(j("ANIME", {"ANIME"}, None, 1, "el")[0])       # too short


class TestUnoParsing(unittest.TestCase):
    def test_parse(self):
        p = uno.parse_card
        self.assertEqual(p(["r5"]), (("R", "5"), None))
        self.assertEqual(p(["🔴5"]), (("R", "5"), None))
        self.assertEqual(p(["red", "skip"]), (("R", "SKIP"), None))
        self.assertEqual(p(["y+2"]), (("Y", "+2"), None))
        self.assertEqual(p(["wild", "blue"]), ((None, "WILD"), "B"))
        self.assertEqual(p(["+4", "g"]), ((None, "+4"), "G"))
        self.assertEqual(p(["nonsense"]), (None, None))
        self.assertEqual(p([]), (None, None))

    def test_deck_is_standard_108(self):
        self.assertEqual(len(uno.build_deck(random.Random(0))), 108)


class TestUnoRules(unittest.TestCase):
    def _game(self, n=3, seed=1):
        g = uno.UnoGame(1, "P1", random.Random(seed))
        for i in range(2, n + 1):
            g.add_player(i, f"P{i}")
        self.assertTrue(g.start()[0])
        return g

    def test_lobby_rules(self):
        g = uno.UnoGame(1, "A")
        self.assertFalse(g.start()[0])           # needs 2 players
        self.assertFalse(g.add_player(1, "A")[0])  # duplicate
        for i in range(2, 7):
            self.assertTrue(g.add_player(i, str(i))[0])
        self.assertFalse(g.add_player(7, "x")[0])  # full
        g.start()
        self.assertFalse(g.add_player(8, "late")[0])  # no joining mid-game

    def test_turn_enforced_and_hand_checked(self):
        g = self._game()
        self.assertFalse(g.play(2, ["r5"]).ok)  # not P2's turn
        self.assertFalse(g.draw_turn(3).ok)

    def test_plus_two_skip_reverse(self):
        g = self._game(3)
        g.players[0].hand = [("R", "+2"), ("R", "1")]
        g.discard, g.color = [("R", "5")], "R"
        self.assertTrue(g.play(1, ["r", "+2"]).ok)
        self.assertEqual(len(g.players[1].hand), 9)      # drew 2
        self.assertEqual(g.current.id, 3)                # P2 skipped
        g.players[2].hand = [("R", "REV"), ("B", "1")]
        self.assertTrue(g.play(3, ["r", "rev"]).ok)
        self.assertEqual(g.direction, -1)

    def test_wild_needs_color_and_sets_it(self):
        g = self._game(2)
        g.players[0].hand = [(None, "WILD"), ("R", "1")]
        self.assertFalse(g.play(1, ["wild"]).ok)
        self.assertTrue(g.play(1, ["wild", "blue"]).ok)
        self.assertEqual(g.color, "B")

    def test_mismatch_rejected_and_win_detected(self):
        g = self._game(2)
        g.discard, g.color = [("R", "5")], "R"
        g.players[0].hand = [("B", "7"), ("R", "9")]
        self.assertFalse(g.play(1, ["b7"]).ok)
        g.players[0].hand = [("R", "9")]
        res = g.play(1, ["r9"])
        self.assertTrue(res.ok and res.winner and res.winner.id == 1)

    def test_deck_reshuffle_and_card_conservation(self):
        for seed in range(100):
            rng = random.Random(seed)
            g = self._game(rng.randint(2, 6), seed)
            for _ in range(1500):
                p = g.current
                opts = [c for c in p.hand if g.playable(c)]
                if opts:
                    c = rng.choice(opts)
                    toks = ([uno.COLOR_EMOJI[c[0]] + c[1].lower()] if c[0]
                            else [c[1].lower(), rng.choice("rgby")])
                    res = g.play(p.id, " ".join(toks).split())
                    self.assertTrue(res.ok, (res.message, toks))
                    if res.winner:
                        break
                else:
                    self.assertTrue(g.draw_turn(p.id).ok)
                total = sum(len(x.hand) for x in g.players) + len(g.deck) + len(g.discard)
                self.assertEqual(total, 108)


if __name__ == "__main__":
    unittest.main()
