"""Example for a trusted host backend issuing a one-time iframe code."""

import argparse
import os
import secrets
import time

from apps.api.auth import sign


def issue_code(*, app_origin: str, host_origin: str, nonce: str, secret: str) -> str:
    if len(secret.encode()) < 32:
        raise ValueError("Use an iframe signing secret of at least 32 bytes")
    if not 16 <= len(nonce) <= 128:
        raise ValueError("Invalid iframe nonce")
    return sign(
        {
            "purpose": "embed_code",
            "aud": app_origin,
            "host": host_origin,
            "nonce": nonce,
            "id": secrets.token_urlsafe(18),
            "exp": int(time.time()) + 60,
        },
        secret.encode(),
    )


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument("app_origin")
    arg_parser.add_argument("host_origin")
    arg_parser.add_argument("nonce")
    args = arg_parser.parse_args()
    print(issue_code(**vars(args), secret=os.environ["EMBED_SIGNING_SECRET"]))
