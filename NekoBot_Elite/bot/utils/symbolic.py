"""
Symbolic math for /simplify /factor /derive /integrate /zeroes /tangent /area (v2.17).

Replaces the external Newton API the Masha bot called (``pynewtonmath``) with a local sympy engine, so nothing
leaves the machine and nothing depends on a third-party service.

Because it evaluates text a stranger typed into a chat, it is built defensively:

  1. ``tokenize`` accepts only digits, letters and ``+ - * / ^ ( ) !`` - no ``.`` attribute access, ``_``,
     brackets, quotes, ``:`` or ``=`` ever reaches the parser.
  2. ``parse_expr`` gets a *restricted* namespace (a handful of sympy constructors and the whitelisted math
     functions), not ``from sympy import *``.
  3. The work runs in a separate worker process with a hard wall-clock timeout (the process is killed, not
     politely asked) and an address-space cap, so ``9**9**9**9`` or a pathological integral cannot freeze or
     exhaust the bot.

``execute`` is the pure, in-process core (also used by the worker and by the tests); ``SymbolicSandbox`` is the
async front door the plugin uses.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

MAX_EXPR_LEN = 160
MAX_OUTPUT_LEN = 3500
MAX_POWER_OPS = 4
MAX_INT_DIGITS = 12
MAX_DECIMAL_CHARS = 30
MAX_EXPONENT = 10_000
DEFAULT_TIMEOUT = 8.0
DEFAULT_MEMORY_MB = 1024

OPS = ("simplify", "factor", "derive", "integrate", "zeroes", "tangent", "area", "fn")
FUNCTION_NAMES = (
    "sin", "cos", "tan", "cot", "sec", "csc", "asin", "acos", "atan", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "sqrt", "log", "ln", "exp", "abs", "factorial",
)


class SymbolicError(Exception):
    """A user-facing problem (bad expression, nothing found, too slow)."""


class SymbolicTimeout(SymbolicError):
    pass


# ── input handling (no sympy needed) ───────────────────────────────────────
_TOKEN = re.compile(r"\d+(?:\.\d+)?|\.\d+|[A-Za-z]+|\*\*|[+\-*/^()!]|\s+")
_REPLACEMENTS = {"×": "*", "·": "*", "÷": "/", "−": "-", "–": "-", "π": "pi", "√": "sqrt"}


def normalize(text: str) -> str:
    for old, new in _REPLACEMENTS.items():
        text = text.replace(old, new)
    return text.strip()


def tokenize(text: str) -> List[str]:
    """The expression's tokens; raises SymbolicError for anything outside the whitelist."""
    text = normalize(text)
    if not text:
        raise SymbolicError("Give me an expression, e.g. x^2+3x.")
    if len(text) > MAX_EXPR_LEN:
        raise SymbolicError(f"That expression is too long (max {MAX_EXPR_LEN} characters).")
    tokens = _TOKEN.findall(text)
    if "".join(tokens) != text:
        raise SymbolicError("I can only read numbers, letters and + - * / ^ ( ) !")
    if text.count("**") + text.count("^") > MAX_POWER_OPS:
        raise SymbolicError("Too many exponents in one expression.")
    significant = [t for t in tokens if not t.isspace()]
    for i, tok in enumerate(significant):
        if tok in ("^", "**") and i + 2 < len(significant) and significant[i + 2] in ("^", "**"):
            raise SymbolicError("Chained exponents like a^b^c aren't supported - use parentheses.")
        if tok.isdigit():
            if len(tok) > MAX_INT_DIGITS:
                raise SymbolicError(f"Numbers can have at most {MAX_INT_DIGITS} digits here.")
            if i > 0 and significant[i - 1] in ("^", "**") and int(tok) > MAX_EXPONENT:
                raise SymbolicError(f"Exponents above {MAX_EXPONENT:,} are too big for me.")
        elif len(tok) > MAX_DECIMAL_CHARS and tok[0].isdigit():
            raise SymbolicError("That decimal has too many digits.")
    return significant


def split_variants(raw: str, sep: str) -> Tuple[Optional[str], str]:
    """``'2|x^3'`` -> ('2', 'x^3'); no separator -> (None, whole)."""
    if sep in raw:
        head, tail = raw.split(sep, 1)
        return head.strip(), tail.strip()
    return None, raw.strip()


# ── engine ─────────────────────────────────────────────────────────────────
def _namespace():
    import sympy as sp

    funcs = {
        "sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "cot": sp.cot, "sec": sp.sec, "csc": sp.csc,
        "asin": sp.asin, "arcsin": sp.asin, "acos": sp.acos, "arccos": sp.acos, "atan": sp.atan, "arctan": sp.atan,
        "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh, "sqrt": sp.sqrt, "log": sp.log, "ln": sp.log,
        "exp": sp.exp, "abs": sp.Abs, "factorial": sp.factorial,
    }
    consts = {"pi": sp.pi, "e": sp.E, "E": sp.E}
    # Names the parser's own transformations emit, plus nothing else from sympy.
    glob = {"Symbol": sp.Symbol, "Integer": sp.Integer, "Float": sp.Float, "Rational": sp.Rational,
            "factorial": sp.factorial, "factorial2": sp.factorial2, "Function": sp.Function}
    return sp, {**funcs, **consts}, glob


def parse(text: str):
    """Text -> sympy expression, via the whitelist and the restricted namespace."""
    tokenize(text)  # validates; the parser is handed the normalised string below
    sp, local, glob = _namespace()
    from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application, parse_expr,
                                            standard_transformations)
    transforms = standard_transformations + (implicit_multiplication_application, convert_xor)
    try:
        return parse_expr(normalize(text), local_dict=dict(local), global_dict=dict(glob), transformations=transforms)
    except (SyntaxError, TypeError, ValueError, NameError, AttributeError, sp.SympifyError, ZeroDivisionError,
            sp.core.sympify.SympifyError) as exc:
        raise SymbolicError("I couldn't understand that expression.") from exc


def _variable(expr):
    """x if present, else the only symbol, else the alphabetically first one (None for a constant)."""
    symbols = sorted(expr.free_symbols, key=lambda s: s.name)
    for s in symbols:
        if s.name == "x":
            return s
    return symbols[0] if symbols else None


def fmt(value) -> str:
    text = str(value).replace("**", "^")
    return text if len(text) <= MAX_OUTPUT_LEN else text[:MAX_OUTPUT_LEN] + "…"


def _approx(sp, value) -> str:
    """' ≈ 1.2345' for numeric results that aren't whole numbers, else ''."""
    try:
        if value.is_number and not value.is_Integer:
            return f" ≈ {sp.N(value, 10)}"
    except (TypeError, ValueError, AttributeError):
        pass
    return ""


def execute(op: str, text: str, name: str = "") -> str:
    """Run one operation and return the formatted answer. Raises SymbolicError for anything the user can fix."""
    if op not in OPS:
        raise SymbolicError("Unknown operation.")
    sp, _, _ = _namespace()
    try:
        if op in ("simplify", "factor"):
            expr = parse(text)
            return fmt(sp.simplify(expr) if op == "simplify" else sp.factor(expr))

        if op == "derive":
            expr = parse(text)
            var = _variable(expr)
            return fmt(sp.diff(expr, var)) if var is not None else "0"

        if op == "integrate":
            expr = parse(text)
            var = _variable(expr) or sp.Symbol("x")
            result = sp.integrate(expr, var)
            if result.has(sp.Integral):
                raise SymbolicError("I couldn't find a closed form for that integral.")
            return fmt(result) + " + C"

        if op == "zeroes":
            expr = parse(text)
            var = _variable(expr)
            if var is None:
                return "No variable to solve for." if expr != 0 else "Every value is a zero."
            solutions = sp.solveset(expr, var, domain=sp.S.Reals)
            if isinstance(solutions, sp.ConditionSet):
                raise SymbolicError("I couldn't find a closed form for those zeroes.")
            if solutions == sp.S.EmptySet:
                return "No real zeroes."
            if isinstance(solutions, sp.FiniteSet):
                try:
                    items = sorted(solutions, key=lambda v: float(sp.N(v)))
                except (TypeError, ValueError):
                    items = list(solutions)
                return ", ".join(fmt(v) for v in items)
            return fmt(solutions)

        if op == "tangent":
            point, body = split_variants(text, "|")
            if point is None:
                raise SymbolicError("Usage: /tangent <x value>|<expression>, e.g. /tangent 2|x^3")
            expr, a = parse(body), parse(point)
            var = _variable(expr) or sp.Symbol("x")
            slope = sp.diff(expr, var).subs(var, a)
            line = sp.expand(slope * (var - a) + expr.subs(var, a))
            return f"y = {fmt(sp.simplify(line))}"

        if op == "area":
            bounds, body = split_variants(text, "|")
            if bounds is None or ":" not in bounds:
                raise SymbolicError("Usage: /area <from>:<to>|<expression>, e.g. /area 2:4|x^3")
            lo_raw, hi_raw = (p.strip() for p in bounds.split(":", 1))
            lo, hi, expr = parse(lo_raw), parse(hi_raw), parse(body)
            var = _variable(expr) or sp.Symbol("x")
            result = sp.simplify(sp.integrate(expr, (var, lo, hi)))
            if result.has(sp.Integral):
                raise SymbolicError("I couldn't find a closed form for that area.")
            return fmt(result) + _approx(sp, result)

        if op == "fn":  # /sin /cos ... : exact value (and a decimal) of name(expression)
            func = _namespace()[1].get(name)
            if func is None:
                raise SymbolicError("Unknown function.")
            result = sp.simplify(func(parse(text)))
            return fmt(result) + _approx(sp, result)
    except SymbolicError:
        raise
    except (RecursionError, MemoryError, OverflowError) as exc:
        raise SymbolicError("That is too big for me to work out.") from exc
    except ValueError as exc:
        if "digits" in str(exc):  # Python's int->str conversion limit
            raise SymbolicError("That result is too big to print.") from exc
        raise SymbolicError("I couldn't work that out.") from exc
    except (TypeError, NotImplementedError, ZeroDivisionError, ArithmeticError, AttributeError) as exc:
        raise SymbolicError("I couldn't work that out.") from exc
    raise SymbolicError("Unknown operation.")  # pragma: no cover


# ── worker process ─────────────────────────────────────────────────────────
# A plain subprocess speaking JSON lines on stdin/stdout. (multiprocessing's "spawn" would re-import the bot's
# __main__ in the child; a separate `python -m bot.utils.symbolic` has no such side effects.)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _worker_loop(memory_mb: int) -> None:
    """Child process: cap memory, import sympy once, then answer one JSON job per line until stdin closes."""
    proto = sys.stdout
    sys.stdout = sys.stderr  # anything sympy prints must not corrupt the protocol
    try:
        import resource
        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass  # not available here; the parent's timeout still bounds the damage
    try:
        _namespace()  # warm the import so the first real request isn't slow
    except ImportError:
        pass  # reported per job below

    def reply(status: str, text: str) -> None:
        proto.write(json.dumps({"status": status, "text": text}) + "\n")
        proto.flush()

    for line in sys.stdin:
        try:
            job = json.loads(line)
            op, text, name = job["op"], job["text"], job.get("name", "")
            if op == "_sleep" and os.environ.get("SYMBOLIC_TEST_OPS") == "1":
                time.sleep(float(text))
                reply("ok", "slept")
            else:
                reply("ok", execute(op, text, name))
        except SymbolicError as exc:
            reply("err", str(exc))
        except ImportError:
            reply("err", "Symbolic math needs the sympy package (pip install sympy).")
        except MemoryError:
            reply("err", "That is too big for me to work out.")
        except BaseException:  # noqa: BLE001 - nothing may end the loop except stdin closing
            reply("err", "I couldn't work that out.")


class SymbolicSandbox:
    """One long-lived worker process; jobs run one at a time; the worker is killed and replaced on timeout."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, memory_mb: int = DEFAULT_MEMORY_MB) -> None:
        self.timeout = timeout
        self.memory_mb = memory_mb
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()

    async def _start(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "bot.utils.symbolic", "--worker", str(self.memory_mb),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(_PROJECT_ROOT), env=env, limit=4 * 1024 * 1024,
        )

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:  # pragma: no cover - SIGKILL does not time out
                pass

    async def run(self, op: str, text: str, name: str = "") -> str:
        # cheap rejection before any process work (area/tangent carry "<bounds>|<expr>")
        tokenize(text.split("|", 1)[1] if op in ("tangent", "area") and "|" in text else text)
        async with self._lock:
            if self._proc is None or self._proc.returncode is not None:
                await self._kill()
                await self._start()
            proc = self._proc
            try:
                proc.stdin.write((json.dumps({"op": op, "text": text, "name": name}) + "\n").encode())
                await proc.stdin.drain()
                raw = await asyncio.wait_for(proc.stdout.readline(), self.timeout)
            except asyncio.TimeoutError as exc:
                await self._kill()
                raise SymbolicTimeout("That took too long, so I stopped. Try something simpler.") from exc
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                await self._kill()
                raise SymbolicError("The math worker stopped unexpectedly - try again.") from exc
            if not raw:  # worker died (e.g. hit the memory cap)
                await self._kill()
                raise SymbolicError("That is too big for me to work out.")
        try:
            msg = json.loads(raw)
        except ValueError as exc:
            await self._kill()
            raise SymbolicError("The math worker answered garbage - try again.") from exc
        if msg.get("status") != "ok":
            raise SymbolicError(str(msg.get("text", "I couldn't work that out.")))
        return str(msg["text"])

    def abandon(self) -> None:
        """Kill the worker without awaiting (its loop may already be closed)."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, RuntimeError):
                pass

    async def close(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        await self._kill()


_default: Optional[SymbolicSandbox] = None
_default_loop: Optional[asyncio.AbstractEventLoop] = None


def get_sandbox() -> SymbolicSandbox:
    """The shared sandbox. A worker's pipes belong to the event loop that created them, so if the running loop
    differs (tests, or a restarted loop) the old worker is abandoned and a fresh sandbox is made."""
    global _default, _default_loop
    loop = asyncio.get_running_loop()
    if _default is None or _default_loop is not loop:
        if _default is not None:
            _default.abandon()
        _default, _default_loop = SymbolicSandbox(), loop
    return _default


def sympy_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("sympy") is not None


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        _worker_loop(int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MEMORY_MB)
