"""Chạy trong container để đo riêng từng bước: resolve vs transcode."""
import subprocess
import time

import yt_dlp

VIDEO_ID = "sZrIbpwjTwk"

t0 = time.time()
ydl_opts = {
    "quiet": True,
    "no_warnings": True,
    "format": "bestaudio/best",
    "noplaylist": True,
}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info(
        f"https://www.youtube.com/watch?v={VIDEO_ID}", download=False
    )
audio_url = info["url"]
t1 = time.time()
print(f"[1] Resolve xong: {t1 - t0:.2f}s")

cmd = [
    "ffmpeg",
    "-loglevel", "error",
    "-i", audio_url,
    "-vn",
    "-acodec", "libmp3lame",
    "-ab", "128k",
    "-t", "15",  # chỉ encode thử 15 giây đầu cho nhanh
    "-f", "mp3",
    "/tmp/test.mp3",
    "-y",
]
proc = subprocess.run(cmd, capture_output=True, text=True)
t2 = time.time()
print(f"[2] Transcode (15s audio) xong: {t2 - t1:.2f}s")
print(f"Tổng: {t2 - t0:.2f}s")

if proc.returncode != 0:
    print("FFMPEG LỖI:")
    print(proc.stderr[-2000:])
else:
    print("ffmpeg OK, file: /tmp/test.mp3")
