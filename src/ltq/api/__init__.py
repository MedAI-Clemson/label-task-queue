"""The web API (FastAPI). Phase 1 milestone 6; only a health check so far."""

from fastapi import FastAPI

app = FastAPI(title="label-task-queue")


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the service is running."""
    return {"status": "ok"}
