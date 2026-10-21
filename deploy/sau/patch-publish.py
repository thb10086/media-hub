"""Patch SAU's Douyin publishing so a post carries its body text and a portrait cover.

Run at container start (entrypoint.sh) against the SAU checkout in /app, or locally with
SAU_APP_DIR pointing at a checkout. Like patch-login.py, every patch applies only while its
anchor is present exactly once and is skipped once its marker is in the file, so re-running
it is harmless.

Upstream's DouYinVideo already fills a description (``desc``) before the tags and can upload
a portrait cover (``thumbnail_portrait_path``), but ``/postVideo`` never passes either: the
only cover it forwards goes in as the *landscape* one, and the description is dropped.

1. route    ``/postVideo`` reads ``desc`` and ``thumbnailPortrait`` (a file staged in
            videoFile/, named like the video) and hands both to post_video_DouYin.
2. call     post_video_DouYin takes them and passes them to DouYinVideo by keyword; bare
            file names resolve under videoFile/, like the videos themselves.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(os.environ.get("SAU_APP_DIR", "/app"))

READ_OLD = """    thumbnail_path = data.get('thumbnail', '')
"""

READ_NEW = """    thumbnail_path = data.get('thumbnail', '')
    # -- patched: the post's body text and a portrait cover for Douyin (sent by media-hub)
    desc = data.get('desc') or None
    thumbnail_portrait = data.get('thumbnailPortrait') or ''
"""

ROUTE_CALL_OLD = """                post_video_DouYin(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, thumbnail_path, productLink, productTitle)
"""

ROUTE_CALL_NEW = """                # -- patched: pass the body text and the portrait cover on
                post_video_DouYin(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, thumbnail_path, productLink, productTitle,
                          desc=desc, thumbnail_portrait_path=thumbnail_portrait)
"""

SIGNATURE_OLD = """                      thumbnail_path = '',
                      productLink = '', productTitle = ''):
"""

SIGNATURE_NEW = """                      thumbnail_path = '',
                      productLink = '', productTitle = '', desc=None, thumbnail_portrait_path=''):
    # -- patched: body text and portrait cover; staged files are named bare, like the videos
    def _staged(name):
        if not name:
            return None
        path = Path(name)
        return str(path if path.is_absolute() else BASE_DIR / "videoFile" / path)
"""

CONSTRUCT_OLD = """            app = DouYinVideo(title, str(file), tags, publish_datetimes[index], cookie, thumbnail_path, productLink, productTitle)
"""

CONSTRUCT_NEW = """            # -- patched: the cover as the portrait one, and the body text
            app = DouYinVideo(title, str(file), tags, publish_datetimes[index], cookie, _staged(thumbnail_path), productLink, productTitle,
                              thumbnail_portrait_path=_staged(thumbnail_portrait_path), desc=desc)
"""


def _patch(path: Path, name: str, old: str, new: str, *, marker: str) -> None:
    if not path.exists():
        print(f"  skip {name}: {path} not found")
        return
    source = path.read_text(encoding="utf-8")
    if marker in source:
        return
    if source.count(old) != 1:
        print(f"  skip {name}: anchor not found exactly once (upstream changed?)")
        return
    path.write_text(source.replace(old, new), encoding="utf-8")
    print(f"  patched {name}")


def main() -> None:
    backend = APP_DIR / "sau_backend.py"
    post_video = APP_DIR / "myUtils" / "postVideo.py"
    _patch(backend, "douyin desc and cover (read)", READ_OLD, READ_NEW, marker="# -- patched: the post's body text")
    _patch(backend, "douyin desc and cover (route)", ROUTE_CALL_OLD, ROUTE_CALL_NEW, marker="# -- patched: pass the body text")
    _patch(post_video, "douyin desc and cover (signature)", SIGNATURE_OLD, SIGNATURE_NEW, marker="# -- patched: body text and portrait cover")
    _patch(post_video, "douyin desc and cover (uploader)", CONSTRUCT_OLD, CONSTRUCT_NEW, marker="# -- patched: the cover as the portrait one")


if __name__ == "__main__":
    main()
