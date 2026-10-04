"""
yt2mp3-stream
-------------
Service nhỏ dùng để:
  1. Tìm bài hát trên YouTube theo tên (GET /search?q=...)
  2. Trả về một URL stream MP3 ổn định (GET /stream/{video_id}.mp3) mà
     thiết bị ESP32 (xiaozhi) có thể fetch & decode trực tiếp qua HTTP.

Không trả thẳng link YouTube gốc cho thiết bị, vì đó là trang video,
không phải audio stream thuần mà firmware giải mã được.
"""

import hashlib
import hmac
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Iterator, Optional

import httpx
import yt_dlp
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("yt2mp3")

app = FastAPI(title="yt2mp3-stream")

# Nơi lưu cache file mp3 đã transcode, để lần phát sau không phải
# tải + encode lại từ đầu (tiết kiệm thời gian chờ + băng thông).
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/app/cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Domain public (HTTPS) mà thiết bị sẽ gọi thẳng vào để tải stream.
# PHẢI là domain thật sau reverse proxy, không phải localhost/IP nội bộ.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Nếu có set, /search sẽ dùng YouTube Data API v3 (nhanh hơn nhiều so với
# cách scrape HTML của yt-dlp). Nếu để trống, tự động fallback về yt-dlp
# như code cũ (chậm hơn nhưng không cần API key).
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Secret dùng để tính X-Dynamic-Key, phải khớp đúng với giá trị hardcode
# trong firmware ESP32 (fork xiaozhi.vn). Cho phép override qua env phòng
# trường hợp firmware đổi secret sau này, nhưng mặc định đúng giá trị đã
# reverse-engineer được từ firmware.
ESP32_SECRET_KEY = os.getenv("ESP32_SECRET_KEY", "your-esp32-secret-key-2024")


def _sanitize_video_id(video_id: str) -> str:
    """Chặn path traversal / injection qua tham số video_id trên URL."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="video_id không hợp lệ")
    return video_id


def _search_via_youtube_api(q: str) -> dict:
    """
    Tìm qua YouTube Data API v3 -- 1 request JSON nhẹ, nhanh hơn nhiều so
    với cách yt-dlp tải + parse cả trang HTML kết quả tìm kiếm.
    Cần YOUTUBE_API_KEY hợp lệ (free quota ~100 lần search/ngày).
    """
    resp = httpx.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "q": q,
            "type": "video",
            "maxResults": 1,
            "key": YOUTUBE_API_KEY,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        logger.error("YouTube API lỗi: %s %s", resp.status_code, resp.text)
        raise HTTPException(
            status_code=502, detail=f"YouTube API lỗi: {resp.status_code}"
        )
    items = resp.json().get("items", [])
    if not items:
        raise HTTPException(status_code=404, detail="không tìm thấy bài hát phù hợp")

    item = items[0]
    video_id = item["id"]["videoId"]
    title = item["snippet"]["title"]
    duration = _get_duration_via_api(video_id)
    return {"video_id": video_id, "title": title, "duration": duration}


def _get_duration_via_api(video_id: str) -> Optional[int]:
    """Gọi thêm 1 lần videos.list để lấy duration thật (search.list không trả field này)."""
    try:
        resp = httpx.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "contentDetails", "id": video_id, "key": YOUTUBE_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return None
        iso_duration = items[0]["contentDetails"]["duration"]  # dạng "PT3M45S"
        return _parse_iso8601_duration(iso_duration)
    except Exception:
        logger.warning("Không lấy được duration cho video_id=%s", video_id)
        return None


def _parse_iso8601_duration(iso: str) -> Optional[int]:
    """Parse "PT1H2M3S" -> tổng số giây, không cần thêm thư viện ngoài."""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return None
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s


def _search_via_ytdlp(q: str) -> dict:
    """Fallback cũ: scrape qua yt-dlp khi không có YOUTUBE_API_KEY. Chậm hơn."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(q, download=False)
    except Exception as e:
        logger.exception("Tìm kiếm thất bại cho query=%r", q)
        raise HTTPException(status_code=502, detail=f"tìm kiếm thất bại: {e}")

    entries = [e for e in (info.get("entries") or [info]) if e]
    if not entries:
        raise HTTPException(status_code=404, detail="không tìm thấy bài hát phù hợp")

    entry = entries[0]
    return {
        "video_id": entry["id"],
        "title": entry.get("title", q),
        "duration": entry.get("duration"),
    }


def _verify_device_headers(
    mac: Optional[str],
    chip_id: Optional[str],
    timestamp: Optional[str],
    dynamic_key: Optional[str],
) -> None:
    """
    Xác thực request từ thiết bị ESP32 theo đúng công thức firmware tính:
        key_material = f"{mac}:{chip_id}:{timestamp}:{ESP32_SECRET_KEY}"
        expected = SHA256(key_material)[:16 byte đầu].hex().upper()

    LƯU Ý: X-Timestamp là "giây từ lúc boot" của thiết bị, KHÔNG phải Unix
    epoch -- nên ở đây chỉ xác thực chữ ký đúng/sai, không kiểm tra độ
    "tươi" (freshness) của timestamp vì không có mốc tuyệt đối để so sánh.
    Nghĩa là về lý thuyết 1 request cũ có chữ ký hợp lệ vẫn replay được --
    chấp nhận được cho use case cá nhân, không phải hệ thống công khai.
    """
    if not all([mac, chip_id, timestamp, dynamic_key]):
        raise HTTPException(
            status_code=401,
            detail="thiếu header xác thực (X-MAC-Address/X-Chip-ID/X-Timestamp/X-Dynamic-Key)",
        )

    key_material = f"{mac}:{chip_id}:{timestamp}:{ESP32_SECRET_KEY}"
    digest = hashlib.sha256(key_material.encode()).digest()[:16]
    expected = digest.hex().upper()

    # So sánh constant-time, tránh timing attack dò từng ký tự chữ ký
    if not hmac.compare_digest(expected, dynamic_key.upper()):
        logger.warning(
            "Xác thực thất bại: mac=%s chip_id=%s timestamp=%s", mac, chip_id, timestamp
        )
        raise HTTPException(status_code=401, detail="chữ ký X-Dynamic-Key không hợp lệ")


@app.get("/search")
def search(q: str):
    """
    Tìm bài hát theo tên trên YouTube, trả về video_id + stream_url sẵn dùng.
    n8n gọi endpoint này trước, lấy stream_url rồi trả ngược cho xiaozhi.
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="thiếu tham số q (tên bài hát)")

    result = (
        _search_via_youtube_api(q) if YOUTUBE_API_KEY else _search_via_ytdlp(q)
    )

    return {
        **result,
        "stream_url": f"{PUBLIC_BASE_URL}/stream/{result['video_id']}.mp3",
    }


@app.get("/stream_pcm")
def stream_pcm(
    song: str,
    artist: str = "",
    x_mac_address: Optional[str] = Header(default=None),
    x_chip_id: Optional[str] = Header(default=None),
    x_timestamp: Optional[str] = Header(default=None),
    x_dynamic_key: Optional[str] = Header(default=None),
):
    """
    Endpoint chính mà self.music.play_song (firmware ESP32) gọi tới.
    Tên "stream_pcm" là do firmware đặt sẵn (không đổi được phía mình) --
    nhưng bản thân endpoint này CHỈ trả JSON metadata, không phải audio
    thật. Audio thật nằm ở "audio_url" trong response, thiết bị tự fetch
    tiếp bằng 1 request khác (endpoint /audio/{video_id}.mp3 bên dưới).
    """
    _verify_device_headers(x_mac_address, x_chip_id, x_timestamp, x_dynamic_key)

    query = f"{song} {artist}".strip()
    if not query:
        raise HTTPException(status_code=400, detail="thiếu tham số song")

    result = (
        _search_via_youtube_api(query) if YOUTUBE_API_KEY else _search_via_ytdlp(query)
    )

    response = {
        "title": result.get("title") or song,
        "artist": artist or "",
        # QUAN TRỌNG: firmware tự nối base_url + audio_url ở MỌI nhánh code
        # -- phải trả path TƯƠNG ĐỐI ("/audio/x.mp3"), không phải URL tuyệt
        # đối đầy đủ ("http://host/audio/x.mp3"), nếu không firmware ghép
        # thành chuỗi hỏng (http://host + http://host/audio/x.mp3) và
        # không phát được. Lỗi này không hiện ra log server vì server đã
        # trả 200 đúng JSON hợp lệ -- chỉ là sai định dạng theo convention
        # riêng mà firmware mong đợi.
        "audio_url": f"/audio/{result['video_id']}.mp3",
    }
    # QUAN TRỌNG: chỉ đưa "duration" vào response nếu có giá trị thật.
    # Một số firmware C/C++ parse JSON nghiêm ngặt, field "null" cho field
    # optional có thể khiến parser lỗi âm thầm (không log) rồi bỏ luôn
    # bước fetch audio_url tiếp theo -- an toàn hơn là bỏ hẳn field đó.
    if result.get("duration") is not None:
        response["duration"] = result["duration"]

    return response


def _iter_file(path: Path, chunk_size: int = 65536) -> Iterator[bytes]:
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _transcode_and_cache(video_id: str, cache_file: Path) -> Iterator[bytes]:
    """
    QUAN TRỌNG: không lấy URL audio rồi để ffmpeg tự fetch trực tiếp -- cách
    đó hay bị YouTube trả 403 Forbidden vì ffmpeg không gửi đúng header
    (User-Agent/Referer) mà CDN googlevideo.com mong đợi, và URL có thể bị
    ràng theo IP request gốc.

    Thay vào đó: để chính yt-dlp tải audio (nó tự lo đúng header/cookie),
    xuất thẳng ra stdout ("-o -"), rồi pipe byte thô đó sang ffmpeg để
    transcode sang mp3. Hai tiến trình nối pipe trực tiếp, không qua file
    tạm ở giữa.
    """
    tmp_file = cache_file.with_suffix(".part")
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    ytdlp_cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "--no-playlist",
        "-o", "-",
        "-q",
        video_url,
    ]
    ffmpeg_cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "128k",
        "-f", "mp3",
        "-",
    ]

    logger.info("Bắt đầu tải+transcode video_id=%s", video_id)
    ytdlp_proc = subprocess.Popen(ytdlp_cmd, stdout=subprocess.PIPE, bufsize=10**6)
    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=ytdlp_proc.stdout,
        stdout=subprocess.PIPE,
        bufsize=10**6,
    )
    # Cho phép yt-dlp_proc nhận SIGPIPE nếu ffmpeg đóng sớm, tránh treo tiến trình
    if ytdlp_proc.stdout:
        ytdlp_proc.stdout.close()

    try:
        with open(tmp_file, "wb") as f:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                yield chunk
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()
        ytdlp_proc.wait()

    if proc.returncode == 0 and ytdlp_proc.returncode == 0:
        tmp_file.rename(cache_file)
        logger.info("Transcode xong, đã lưu cache video_id=%s", video_id)
    else:
        tmp_file.unlink(missing_ok=True)
        logger.error(
            "Lỗi tải/transcode video_id=%s (yt-dlp=%s, ffmpeg=%s)",
            video_id, ytdlp_proc.returncode, proc.returncode,
        )


def _serve_audio(video_id: str) -> StreamingResponse:
    video_id = _sanitize_video_id(video_id)
    cache_file = CACHE_DIR / f"{video_id}.mp3"

    if cache_file.exists():
        logger.info("Phát từ cache video_id=%s", video_id)
        return StreamingResponse(_iter_file(cache_file), media_type="audio/mpeg")

    return StreamingResponse(
        _transcode_and_cache(video_id, cache_file),
        media_type="audio/mpeg",
    )


@app.get("/stream/{video_id}.mp3")
def stream(video_id: str):
    """
    Endpoint KHÔNG xác thực -- giữ lại để tiện test tay bằng curl/trình
    duyệt trong lúc debug. Đây không phải endpoint firmware thật sự gọi.
    """
    return _serve_audio(video_id)


@app.get("/audio/{video_id}.mp3")
def audio(
    video_id: str,
    x_mac_address: Optional[str] = Header(default=None),
    x_chip_id: Optional[str] = Header(default=None),
    x_timestamp: Optional[str] = Header(default=None),
    x_dynamic_key: Optional[str] = Header(default=None),
):
    """
    Endpoint audio_url mà /stream_pcm trả về cho firmware. Request này
    cũng mang đúng 4 header xác thực (theo đúng spec), kiểm tra lại ở đây.
    """
    _verify_device_headers(x_mac_address, x_chip_id, x_timestamp, x_dynamic_key)
    return _serve_audio(video_id)


@app.get("/health")
def health():
    return {"status": "ok"}
