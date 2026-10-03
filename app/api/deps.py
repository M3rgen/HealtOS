from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from app.core.config import Settings
from app.core.host_validation import hostname
from app.services.runtime import Runtime


def runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def settings(request: Request) -> Settings:
    return request.app.state.settings


def require_job_request(
    request: Request,
    request_marker: str | None = Header(
        None,
        alias="X-HealthOS-Request",
        description="Required value: 1. Marks a deliberate job-start request for CSRF protection.",
        examples=["1"],
    ),
) -> None:
    """Require deliberate API calls, rejecting browser cross-origin job starts."""
    if request_marker != "1" or request.headers.getlist("x-healthos-request") != ["1"]:
        raise HTTPException(403, "X-HealthOS-Request: 1 is required")
    if any(value.lower() == "cross-site" for value in request.headers.getlist("sec-fetch-site")):
        raise HTTPException(403, "Cross-site job requests are not allowed")
    origins = request.headers.getlist("origin")
    if not origins:
        return  # Non-browser clients must still supply the explicit request header.
    valid = False
    if len(origins) == 1:
        try:
            origin = urlsplit(origins[0])
            target = urlsplit(f"{request.url.scheme}://{request.headers.get('host', '')}")
            default_port = 443 if target.scheme == "https" else 80
            valid = (
                origin.scheme in {"http", "https"}
                and origins[0] == f"{origin.scheme}://{origin.netloc}"
                and hostname(origin.netloc) is not None
                and origin.scheme == target.scheme
                and origin.hostname == target.hostname
                and (origin.port or default_port) == (target.port or default_port)
                and origin.username is None
                and origin.password is None
                and not (origin.path or origin.query or origin.fragment)
            )
        except ValueError:
            pass
    if not valid:
        raise HTTPException(403, "A same-origin job request is required")
