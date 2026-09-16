"""
run_state.py
------------
A per-page checkpoint, so a run that dies on page 17 of 20 does not start
again at page 1.

WRITTEN ALWAYS, NOT BEHIND A FLAG. A checkpoint you have to opt into BEFORE
the failure is worthless: nobody passes `--checkpoint` on the run that is
about to be killed, and by the time they want it the pages are gone. So a
multi-page run always writes one, `--resume` reads it, and a run that
finishes completely deletes it — a stale checkpoint beside a finished run
would otherwise offer to resume something that is already done.

Single-page runs write nothing: there is no page to resume to.

WHAT IT IS NOT. This is not the output. The output contract is untouched —
a run that finds nothing still writes nothing, `save` still refuses to
overwrite good data with `[]`, and the sidecar is still only written beside a
file that exists. The checkpoint is scratch state for one run, named
`<out>.progress.json`, and .gitignore'd like the rest of a run's working
files.

THE IDENTITY CHECK IS THE WHOLE RISK. Resuming the wrong run silently
produces a file mixing two categories, which is worse than any crash: it
looks like a successful scrape of something that was never scraped. So the
checkpoint records what the pages are OF — the start URL, the page count and
the category label — and `load` refuses anything that does not match,
naming the difference rather than quietly starting over.

Deliberately NOT part of that identity: --format, --out, --delay, --retries,
--proxy, --concurrency. None of them changes what a page CONTAINS, and
including them would refuse a resume for a reason that does not affect the
data — the commonest thing a person changes between the crash and the retry
is exactly the retry/proxy settings.

Credentials never reach the file: only the three identity fields are stored,
and a URL carrying userinfo is masked on the way in.
"""

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from output_writer import Product

# The schema version of the file itself. A checkpoint written by an older
# build whose Product had different fields must be refused, not fed to a
# dataclass that will raise TypeError on an unexpected key halfway through a
# resume. Bump when Product's fields change.
FORMAT_VERSION = 1


def path_for(out_prefix: str) -> str:
    return f"{out_prefix}.progress.json"


def _mask_url(url: str) -> str:
    """Strip any user:pass@ from a URL before it is written to disk.

    A listing URL should never carry credentials, which is exactly why it is
    worth stripping: the one that does is the one nobody expected.
    """
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=host))


def identity(args) -> dict:
    """What the checkpoint's pages are OF. See the module docstring."""
    return {
        "start_url": _mask_url(args.url or ""),
        "pages_requested": args.pages,
        "category": args.category,
        # The MODE belongs to identity even though it is not a URL: a listing
        # checkpoint holds one row per product and a detail checkpoint one row
        # per size, so resuming one as the other would restore rows of the
        # wrong shape into a run that then writes them as if they fitted.
        "mode": getattr(args, "mode", "listing"),
    }


class Checkpoint:
    """Per-page results for one run, persisted after each page.

    Held as a dict keyed by page NUMBER, never a list in arrival order: the
    output has to merge in page order regardless of which page happened to
    finish first (see PageOutcome), and a resumed run mixes pages restored
    from disk with pages fetched now — so arrival order is not even
    well-defined across the two.
    """

    def __init__(self, out_prefix: str, args, row_type=Product):
        self.path = path_for(out_prefix)
        self.identity = identity(args)
        # Which dataclass the stored rows rebuild into. Hardcoding Product
        # here would raise TypeError halfway through resuming a detail run, on
        # the first unexpected key — after the file had already been read and
        # the run had already announced it was resuming.
        self.row_type = row_type
        self.pages: Dict[int, List[Product]] = {}
        self.final_urls: Dict[int, Optional[str]] = {}
        self.resumed_from: List[int] = []
        # Written only for multi-page runs; a single page has nothing to
        # resume to, and the file would be pure litter.
        self.enabled = args.pages > 1

    # -- writing -------------------------------------------------------

    def record(self, page_num: int, products: List[Product],
               final_url: Optional[str] = None) -> None:
        """Remember one page's result and flush.

        Flushed per page rather than at the end, which is the only version
        that helps: the run this exists for is the one that does not reach
        the end. The cost is one small file write per page, against a page
        fetch measured in seconds.
        """
        self.pages[page_num] = list(products)
        self.final_urls[page_num] = final_url
        if self.enabled:
            self._flush()

    def _flush(self) -> None:
        payload = {
            "format_version": FORMAT_VERSION,
            "identity": self.identity,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pages": {
                str(n): {
                    "final_url": self.final_urls.get(n),
                    "products": [asdict(p) for p in self.pages[n]],
                }
                for n in sorted(self.pages)
            },
        }
        # Written via a temporary file and replaced atomically: the process
        # this guards against is one that dies mid-run, and dying midway
        # through the write would leave truncated JSON that `load` could only
        # reject — turning the crash it exists for into a lost checkpoint.
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        """Remove the checkpoint. Called only when the run COMPLETED.

        A stale file beside a finished run would offer to resume something
        already done, and the resume would look like a successful scrape
        while fetching nothing.
        """
        for p in (self.path, f"{self.path}.tmp"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    # -- reading -------------------------------------------------------

    def resume(self) -> List[str]:
        """Load a matching checkpoint, if one is there. Returns warnings.

        Returning the reasons rather than logging them keeps this module free
        of a logger and lets the caller decide how loudly to complain — but
        every path that declines to resume produces a reason, because
        "resume did nothing" with no explanation is the failure this whole
        file is trying to avoid.
        """
        if not os.path.exists(self.path):
            return [f"No checkpoint at {self.path} — starting from page 1."]
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return [f"Checkpoint {self.path} is unreadable ({e}) — starting "
                    f"from page 1."]

        if data.get("format_version") != FORMAT_VERSION:
            return [f"Checkpoint {self.path} was written by a different "
                    f"version of this tool (format {data.get('format_version')}, "
                    f"this build writes {FORMAT_VERSION}) — starting from "
                    f"page 1 rather than restoring rows whose columns may "
                    f"not match."]

        stored = data.get("identity") or {}
        if stored != self.identity:
            differing = sorted(
                k for k in set(stored) | set(self.identity)
                if stored.get(k) != self.identity.get(k))
            details = "; ".join(
                f"{k}: checkpoint has {stored.get(k)!r}, this run has "
                f"{self.identity.get(k)!r}" for k in differing)
            return [f"Checkpoint {self.path} is for a DIFFERENT run "
                    f"({details}) — ignoring it and starting from page 1. "
                    f"Resuming it would merge two categories into one file."]

        restored = []
        for num_s, page in (data.get("pages") or {}).items():
            try:
                num = int(num_s)
                self.pages[num] = [self.row_type(**row)
                                  for row in page["products"]]
                self.final_urls[num] = page.get("final_url")
                restored.append(num)
            except (TypeError, ValueError, KeyError) as e:
                return [f"Checkpoint {self.path} holds a page this build "
                        f"cannot read ({e}) — starting from page 1."]

        self.resumed_from = sorted(restored)
        if not self.resumed_from:
            return [f"Checkpoint {self.path} holds no completed pages — "
                    f"starting from page 1."]
        return [f"Resuming: pages {_ranges(self.resumed_from)} restored from "
                f"{self.path}; they will not be fetched again."]

    # -- what the caller needs -----------------------------------------

    def has(self, page_num: int) -> bool:
        return page_num in self.pages

    def products_in_page_order(self) -> List[Product]:
        out = []
        for n in sorted(self.pages):
            out.extend(self.pages[n])
        return out


def _ranges(numbers: List[int]) -> str:
    """"1, 2, 3, 7" -> "1-3, 7". Page lists get long; a reader wants the shape."""
    if not numbers:
        return "none"
    spans, start, prev = [], numbers[0], numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append((start, prev))
        start = prev = n
    spans.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)
