"""Serve the app under a public path prefix added by reverse proxies."""

import html


def prefix_variants(base_path: str):
    """The public prefix and each of its trailing segment runs, longest first.

    A proxy chain may strip any leading part of the public prefix before the request
    reaches the app (for example /ai/labs/project/app arriving as /project/app).
    """
    segments = base_path.strip("/").split("/") if base_path else []
    return ["/" + "/".join(segments[index:]) for index in range(len(segments))]


class StripBasePath:
    """Map /<prefix>/... to /... so routes stay unprefixed; unprefixed paths pass through."""

    def __init__(self, app, base_path: str):
        self.app = app
        self.variants = prefix_variants(base_path)

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"} and self.variants:
            path = scope["path"]
            for prefix in self.variants:
                if path == prefix or path.startswith(prefix + "/"):
                    stripped = path[len(prefix):] or "/"
                    raw = scope.get("raw_path")
                    scope = dict(scope, path=stripped)
                    if isinstance(raw, bytes) and raw.startswith(prefix.encode()):
                        scope["raw_path"] = raw[len(prefix.encode()):] or b"/"
                    else:
                        scope.pop("raw_path", None)
                    break
        await self.app(scope, receive, send)


def with_base_tag(document: str, base_path: str):
    """Resolve the build's relative asset paths and page links against the public prefix."""
    if not base_path:
        return document
    tag = f'<base href="{html.escape(base_path, quote=True)}/">'
    head = document.find("<head>")
    if head < 0:
        return tag + document
    return document[:head + len("<head>")] + tag + document[head + len("<head>"):]
