# Builds the Playwright engine (the one the README recommends) into a
# container with its own Chromium — for a CI canary run or a scheduled job,
# not required for local development (`pip install` directly is simpler there).
#
#   docker build -t farfetch-scraper .
#   docker run --rm -v "$PWD/out:/out" farfetch-scraper \
#     --url "https://www.farfetch.com/shopping/kids/girls-clothing-4/items.aspx" \
#     --pages 1 --out /out/girls_clothing
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env — nothing here bakes in a credential.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    # Playwright's own apt-get for Chromium's shared-library dependencies —
    # not pip packages, so this has to run as a separate, explicit step.
    && playwright install --with-deps chromium

COPY captcha_solver.py env_config.py fingerprint_client.py output_writer.py \
     playwright_scraper.py product_parser.py diff_runs.py ./

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
