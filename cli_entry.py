"""
cli_entry.py
------------
Targets for the console scripts declared in pyproject.toml.

They exist for one reason: the engines import their drivers at MODULE level,
on purpose, and the console scripts are installed for all of them at once.

Module-level driver imports are not an accident to be tidied away — the
offline suite detects an absent engine by catching ImportError on the
module, and a sibling repo once moved `launch`/`connect` inside the launch
path, which made the module import cleanly with no pyppeteer installed: the
engine group never skipped, the CI job that exists to fail on unexpected
skips could not have caught a broken import, and CI ran against a stub
version for a while with nothing noticing (CLAUDE.md §10). So the import
stays where it is.

But `pip install .[playwright]` — installing exactly one engine, which is
what the README tells you to do and what the extras are FOR — still creates
`farfetch-scraper-selenium` on your PATH. Running it produced:

    Traceback (most recent call last):
      ...
      File ".../selenium_scraper.py", line 41, in <module>
        from selenium import webdriver
    ModuleNotFoundError: No module named 'selenium'

which is a correct diagnosis buried in a stack trace, for a command the
install itself created. This layer catches exactly that import failure and
says what to install instead. Any OTHER ImportError is re-raised untouched:
a genuinely broken module must not be reported as "you forgot an extra".
"""

import importlib
import sys

# engine module -> (driver distribution to import, extra that installs it).
# The driver name is what appears in the ModuleNotFoundError; the extra is
# what the user types. They differ for pyppeteer, which is why this is a map
# rather than a string operation on the module name.
_ENGINES = {
    "playwright_scraper": ("playwright", "playwright"),
    "selenium_scraper": ("selenium", "selenium"),
    "puppeteer_scraper": ("pyppeteer", "puppeteer"),
}


def _run(module_name: str) -> int:
    driver, extra = _ENGINES.get(module_name, (None, None))
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        # Only the engine's OWN driver. `e.name` is the module that was
        # actually missing, so a missing bs4 — or a typo in an import inside
        # the engine — still surfaces as itself rather than being blamed on
        # an uninstalled extra.
        if driver is None or e.name != driver:
            raise
        print(f"This command needs the {driver} engine, which is not "
              f"installed.\n\n"
              f"    pip install '.[{extra}]'\n"
              f"  or\n"
              f"    pip install -r requirements.txt -r requirements-{extra}.txt\n",
              file=sys.stderr)
        if extra == "playwright":
            print("Then: playwright install chromium\n", file=sys.stderr)
        print(f"Install exactly ONE engine per environment: the three pin "
              f"mutually unsatisfiable versions of pyee and urllib3, so pip "
              f"may resolve a conflict by quietly downgrading one of them. "
              f"Use a separate virtualenv if you need more than one.",
              file=sys.stderr)
        return 2
    return module.main()


def playwright() -> int:
    return _run("playwright_scraper")


def selenium() -> int:
    return _run("selenium_scraper")


def puppeteer() -> int:
    return _run("puppeteer_scraper")
