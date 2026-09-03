#!/usr/bin/env python3
"""Mint LiveKit access tokens for devices and test clients.

Examples:
    # token for the reSpeaker XVF3800 device
    python3 scripts/token.py --identity respeaker-1 --room home --name "reSpeaker"

    # token for the browser test client
    python3 scripts/token.py --identity web-1 --room home --name "Web"

Credentials come from the environment (LIVEKIT_API_KEY / LIVEKIT_API_SECRET)
or can be passed with --api-key / --api-secret.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

from livekit import api


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a LiveKit access token")
    parser.add_argument("--identity", default="device-1", help="participant identity")
    parser.add_argument("--name", default=None, help="display name")
    parser.add_argument("--room", default="home", help="room to join")
    parser.add_argument("--api-key", default=os.getenv("LIVEKIT_API_KEY", "devkey"))
    parser.add_argument(
        "--api-secret", default=os.getenv("LIVEKIT_API_SECRET", "")
    )
    parser.add_argument(
        "--valid-hours", type=int, default=12, help="token lifetime in hours"
    )
    parser.add_argument(
        "--no-publish", action="store_true", help="disallow publishing (listen only)"
    )
    parser.add_argument(
        "--ws-url", default=os.getenv("PUBLIC_LIVEKIT_WS_URL", ""), help="print connect URL hint"
    )
    args = parser.parse_args()

    if not args.api_secret:
        print("error: LIVEKIT_API_SECRET not set (use --api-secret or .env)", file=sys.stderr)
        return 1

    grants = api.VideoGrants(
        room_join=True,
        room=args.room,
        can_publish=not args.no_publish,
        can_subscribe=True,
        can_publish_data=True,
    )
    token = (
        api.AccessToken(args.api_key, args.api_secret)
        .with_identity(args.identity)
        .with_name(args.name or args.identity)
        .with_grants(grants)
        .with_ttl(timedelta(hours=args.valid_hours))
        .to_jwt()
    )

    print(f"# room    : {args.room}")
    print(f"# identity: {args.identity}")
    if args.ws_url:
        print(f"# url     : {args.ws_url}")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
