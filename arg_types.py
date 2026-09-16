"""
arg_types.py
------------
argparse `type=` callables for the numeric flags every CLI here shares.

Why these exist, measured rather than assumed. Before them, `--retries 0`
was accepted by all three browser engines, and the attempt loop is

    for attempt in range(1, args.retries + 1):

so zero attempts means the loop body never runs and `page.goto()` is never
called. The run then parsed `about:blank` — 39 bytes, confirmed on
2026-09-14 against a page that returns 559 bytes with `--retries 1` — found
no products in it, and exited 4: "the page was fetched and held nothing".

That is this codebase's worst bug class (doing less than it says while
reporting success), reached by typing a number. `--pages 0`, `--concurrency
0` and a negative `--delay` are smaller versions of the same thing: a value
that looks configurable, is accepted, and quietly changes what the run means.

A `type=` callable rather than a post-parse `validate(args)` function:
argparse then produces the usage message and exit 2 itself, the constraint
is written at the flag's own definition where someone editing it will see
it, and a bad value is rejected before any browser is launched. The drift
risk — a new numeric flag added without one — is closed by a check in
smoke_test.py that walks every CLI's AST and asserts each `type=int`/`float`
argument uses one of these.

The distinction that matters is between "zero is a legitimate setting" and
"zero silently disables the thing":

    --delay 0                 no pause between pages. A real choice.
    --retry-delay 0           retry immediately. A real choice.
    --proxy-block-retries 0   don't rotate on a block. A real choice, and
                              already what the code does with no pool.
    --retries 0               never fetch the page at all. Not a choice
                              anyone means; see above.
    --pages 0                 fetch... one page, because page 1 is always
                              fetched on its own. The number would simply
                              not describe the run.
    --concurrency 0           max(1, N) silently makes it 1.

So zero is allowed exactly where it names a real behaviour, and refused
where it names none.
"""

import argparse


def positive_int(raw: str) -> int:
    """An integer of at least 1 — a count of things that must happen."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"must be 1 or more, not {value} (zero or negative would mean the "
            f"run does not happen at all, which is never what is wanted — see "
            f"arg_types.py)")
    return value


def nonneg_int(raw: str) -> int:
    """An integer of at least 0 — a count where 0 names a real behaviour."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not a whole number") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"cannot be negative, got {value}")
    return value


def nonneg_float(raw: str) -> float:
    """Seconds. 0 means "no pause", which is a real choice; below 0 is not.

    Left unguarded this reached `time.sleep(-1)`, which raises ValueError and
    ends the run as a crash (exit 1) partway through — losing whatever had
    been gathered, and reported as a bug in this code rather than as the typo
    it is.
    """
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not a number") from None
    if value < 0:
        raise argparse.ArgumentTypeError(
            f"is a number of seconds and cannot be negative, got {value}")
    return value


def bounded_int(low: int, high: int):
    """Factory for a flag whose range a REMOTE service fixes, not us.

    Used for the Scraper API's `timeout`, which the API caps at 120 and
    rejects outside 1..120. Catching it here turns a paid round-trip ending
    in HTTP 422 into an immediate usage error naming the real limit.
    """
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{raw!r} is not a whole number") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"must be between {low} and {high}, got {value}")
        return value
    parse.__name__ = f"bounded_int_{low}_{high}"
    return parse


# Every callable above, by name. smoke_test.py reads this to check that no
# numeric flag in any CLI was added with a bare `type=int`/`type=float`.
VALIDATORS = frozenset({"positive_int", "nonneg_int", "nonneg_float"})

# Numeric flags deliberately left with a bare type, with the reason. A flag
# here is a DECISION; a flag missing from both this set and VALIDATORS is an
# oversight, and the suite fails on it.
UNVALIDATED_OK = {
    # Constrained by `choices=[0.3, 0.7, 0.9]` instead, which is stronger
    # than any range: the 2Captcha API accepts exactly those three values for
    # a v3 score, so a range check would still let 0.5 through to buy a token
    # the site rejects.
    "--min-score",
}
