# The campaign service (flpcr.service.main): operator + sequencer + read API for one
# distributor. It boots by resyncing from chain, so nothing on disk is required; mount a
# volume at /app/data to keep the LP crawl cache across deploys.
FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir chia-wallet-sdk==0.36.0

# Only what the service runs: the package and the compiled, pinned puzzles.
COPY flpcr ./flpcr
COPY contracts/compiled ./contracts/compiled
COPY contracts/pins.json ./contracts/pins.json

ENV PYTHONUNBUFFERED=1 FLPCR_CACHE_DIR=/app/data
EXPOSE 8080
CMD ["python", "-m", "flpcr.service.main"]
