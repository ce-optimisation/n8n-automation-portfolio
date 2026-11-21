"""Serve review videos with Windows delete sharing while a browser is playing them."""
import os
from pathlib import Path

from fastapi.responses import FileResponse, StreamingResponse, Response


def review_video(path: Path, request):
    if os.name != "nt":
        return FileResponse(path, media_type="video/mp4")

    import _winapi
    import msvcrt
    from starlette.background import BackgroundTask

    # Browser playback may outlive the review card. FILE_SHARE_DELETE lets the owner
    # remove the directory entry while the current stream finishes on its existing handle.
    handle = _winapi.CreateFile(str(path), 0x80000000, 7, 0, 3, 0x80, 0)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise
    file = os.fdopen(fd, "rb")
    try:
        stat = os.fstat(fd)
        base = FileResponse(path, media_type="video/mp4", stat_result=stat)
        headers = dict(base.headers)
        size = stat.st_size
        start, end, status = 0, size, 200
        range_header = request.headers.get("range", "")
        validator = request.headers.get("if-range")
        if range_header.startswith("bytes=") and "," not in range_header and (
                not validator or validator in (headers["etag"], headers["last-modified"])):
            import re
            bounds = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if bounds and any(bounds.groups()):
                first, last = bounds.groups()
                start = int(first) if first else max(0, size - int(last))
                end = min(size, int(last) + 1) if first and last else size
                if start >= end:
                    file.close()
                    return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
                status = 206
                headers.update({"content-range": f"bytes {start}-{end-1}/{size}", "content-length": str(end-start)})
        file.seek(start)

        def chunks():
            remaining = end-start
            try:
                while remaining:
                    chunk = file.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                file.close()

        return StreamingResponse(chunks(), status_code=status, headers=headers, background=BackgroundTask(file.close))
    except BaseException:
        file.close()
        raise
