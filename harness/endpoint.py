"""Checking the model endpoint before a battle starts.

A battle is long and mostly unattended, so it is worth a second up front to confirm the endpoint
is reachable, serves the model that was asked for, and has a context window big enough for what
the agent intends to send. Finding any of that out on turn 14 is much more annoying.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class EndpointError(RuntimeError):
    pass


class EndpointInfo:
    def __init__(self, model: str, context_window: int | None, served: list[str]) -> None:
        self.model = model
        self.context_window = context_window
        self.served = served


def verify(base_url: str, model: str, api_key: str | None, timeout: float = 15.0) -> EndpointInfo:
    """Confirms the endpoint serves 'model'. Raises EndpointError with something actionable."""
    url = base_url.rstrip("/") + "/models"

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise EndpointError(f"{url} answered HTTP {exc.code}. Is it an OpenAI-compatible endpoint?") from exc
    except urllib.error.URLError as exc:
        raise EndpointError(f"cannot reach {url}: {exc.reason}. Is the server running?") from exc
    except (ValueError, KeyError) as exc:
        raise EndpointError(f"{url} did not return a model list: {exc}") from exc

    entries = body.get("data") or body.get("models") or []
    served = [entry.get("id") or entry.get("name") for entry in entries if isinstance(entry, dict)]
    served = [name for name in served if name]

    if model not in served:
        raise EndpointError(f"the endpoint does not serve '{model}'. It serves: {', '.join(served) or '(nothing)'}")

    # llama.cpp reports the window under meta.n_ctx. Other servers do not report it at all, which
    # is not an error: it only means the agent's budget cannot be checked against reality.
    context_window = None
    for entry in entries:
        if isinstance(entry, dict) and (entry.get("id") == model or entry.get("name") == model):
            meta = entry.get("meta") or {}
            if isinstance(meta, dict) and isinstance(meta.get("n_ctx"), int):
                context_window = meta["n_ctx"]
            break

    return EndpointInfo(model, context_window, served)
