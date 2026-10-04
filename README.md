# yt2mp3-stream

Service nhỏ: tìm bài hát trên YouTube theo tên, transcode sang MP3, phát
qua HTTP stream để thiết bị ESP32 (xiaozhi) fetch & decode trực tiếp được.

## Vì sao cần service này

Link YouTube gốc (`youtube.com/watch?v=...`) là trang video, ESP32 không mở
được — nó chỉ phát được khi có một URL trả về đúng audio thô (ở đây dùng
MP3). Service này đứng giữa, lo phần search + transcode đó.

## Kiến trúc

```
GET /search?q=<tên bài>
      │
      ├─ Có YOUTUBE_API_KEY  → gọi YouTube Data API v3 (nhanh, <1s)
      └─ Không có            → fallback yt-dlp scrape HTML (chậm, ~45s)
      │
      ▼
   trả về {video_id, title, stream_url}

GET /stream/{video_id}.mp3
      │
      ├─ Đã có cache → trả thẳng file
      └─ Chưa có     → yt-dlp tải audio (stdout) | pipe | ffmpeg transcode mp3
                        vừa stream ra client ngay, vừa ghi cache song song
```

## Cài đặt & chạy

### 1. Lấy YouTube Data API key (khuyên dùng, nhanh hơn ~45 lần so với không có key)

1. Vào https://console.cloud.google.com/apis/credentials
2. Bật **YouTube Data API v3** (APIs & Services → Library → tìm → Enable)
3. **+ CREATE CREDENTIALS → API key** (KHÔNG chọn "OAuth client ID" — đó là
   loại khác, không dùng được cho việc này)
4. **Application restrictions: để "None"** — restrict theo IP dễ bị lỗi
   `API_KEY_IP_ADDRESS_BLOCKED` nếu VPS dùng NAT/CGNAT có IP outbound không
   cố định (đã gặp thực tế: cùng 1 container, IP outbound đổi qua lại giữa
   2 địa chỉ khác nhau giữa các lần gọi). Quota free tier thấp (~100
   search/ngày) nên rủi ro nếu lộ key không lớn.

### 2. Cấu hình

```bash
cp .env.example .env
nano .env   # điền PUBLIC_BASE_URL và YOUTUBE_API_KEY
```

### 3. Chạy bằng Docker Compose

```bash
docker compose up -d --build
```

### 4. Test

```bash
curl -s -G "http://localhost:8000/search" --data-urlencode "q=ten bai hat"
# -> {"video_id": "...", "title": "...", "stream_url": "http://.../stream/XXXXXXXXXXX.mp3"}

curl -s -o test.mp3 -w "status=%{http_code} size=%{size_download}\n" \
  "http://localhost:8000/stream/XXXXXXXXXXX.mp3"
# status=200, size vài MB -> thành công. Nghe thử test.mp3 để chắc có tiếng.
```

## Tích hợp trực tiếp với firmware xiaozhi.vn (self.music.play_song) — KHÔNG CẦN n8n

Sau khi reverse-engineer được spec thật của `self.music.play_song`, service
này đã implement đúng 100% giao thức firmware cần — **bỏ qua hẳn n8n/bridge**
cho riêng tính năng phát nhạc (n8n/bridge vẫn hữu ích cho các skill khác).

### Giao thức

Firmware gọi `GET <base_url>/stream_pcm?song=<tên>&artist=<ca sĩ>`, kèm 4 header:

| Header | Ý nghĩa |
|---|---|
| `X-MAC-Address` | MAC address thiết bị |
| `X-Chip-ID` | Chip ID thiết bị |
| `X-Timestamp` | Giây tính từ lúc boot |
| `X-Dynamic-Key` | 32 hex chữ hoa = `SHA256("<mac>:<chipid>:<timestamp>:your-esp32-secret-key-2024")[:16 byte đầu]` |

Server trả JSON `{"title", "artist", "audio_url", "lyric_url"?, "duration"?}`.
Thiết bị fetch tiếp `audio_url` (cũng kèm đúng 4 header đó) để stream MP3 thật.

**QUAN TRỌNG — `audio_url` phải là path TƯƠNG ĐỐI** (ví dụ `/audio/xxxxx.mp3`),
KHÔNG phải URL tuyệt đối đầy đủ (`http://host/audio/xxxxx.mp3`). Firmware tự
nối `base_url + audio_url` ở mọi nhánh code — nếu server trả URL tuyệt đối,
firmware ghép thành chuỗi hỏng (`base_url` + `http://host/...`) và không phát
được, dù server trả response hoàn toàn hợp lệ (200 OK, JSON đúng cấu trúc) —
lỗi này không hề lộ ra trong log server, chỉ phát hiện được khi test qua
đúng thiết bị thật hoặc đọc kỹ cách firmware xử lý response.

### Cấu hình

Việc còn lại chỉ là **trỏ `base_url` trong cấu hình firmware** (menuconfig
hoặc web config panel của board, tuỳ fork xiaozhi.vn expose chỗ nào) về
đúng IP/domain VPS của anh, ví dụ:
```
http://your-vps-ip:8000
```
(hoặc domain HTTPS khi đã gắn xong — khuyến khích dùng domain HTTPS cho ổn
định hơn, một số mạng WiFi chặn hẳn HTTP thường)

### Test trước khi đụng thiết bị thật

Dùng `test_device_auth.py` để tự sinh header hợp lệ, test bằng curl:
```bash
python3 test_device_auth.py "ten bai hat" "ten ca si"
# copy lệnh curl được in ra, chạy thử
```

Nếu thấy JSON `{"title": ..., "audio_url": "http://.../audio/XXXXX.mp3", ...}`
→ xác thực hoạt động đúng. Test tiếp request không có header phải bị từ
chối `401`.

### Nếu ESP32_SECRET_KEY firmware dùng khác giá trị mặc định

Set qua `.env`:
```
ESP32_SECRET_KEY=gia-tri-thuc-te-neu-khac
```

## (Tuỳ chọn) Nối vào n8n cho các skill khác

Nếu vẫn muốn dùng n8n cho các tool khác ngoài nhạc (bật đèn, hỏi thời
tiết...), kiến trúc bridge → n8n webhook vẫn áp dụng bình thường như đã
dựng trước đó, không liên quan tới phần `/stream_pcm` này.

## Troubleshooting — các lỗi thực tế đã gặp khi dựng service này

**`/search` mất 40-50 giây** → DNS resolve bên trong container chậm (đã đo
thực tế 8s chỉ để resolve 1 domain). Service này đã tự cấu hình `dns:
8.8.8.8, 1.1.1.1` trong `docker-compose.yml` để tránh lỗi này — nếu vẫn
chậm, kiểm tra lại DNS có áp dụng đúng chưa:
```bash
docker exec yt2mp3 python3 -c "import socket,time; t=time.time(); print(socket.gethostbyname('www.googleapis.com')); print(time.time()-t)"
```

**`API_KEY_INVALID`** → đã dán nhầm OAuth Client Secret (dạng `GOCSPX-...`)
thay vì API key thật (dạng `AIzaSy...`). Hai loại credential khác nhau
hoàn toàn trong Google Cloud Console, tạo nhầm trang rất dễ xảy ra.

**`API_KEY_IP_ADDRESS_BLOCKED`** → key bị giới hạn theo IP nhưng IP
outbound thật của container không khớp (đặc biệt nếu mạng có NAT/CGNAT).
Đổi "Application restrictions" về "None" trong Credentials.

**`ffmpeg` báo `403 Forbidden` khi fetch audio URL** → đây là lỗi hay gặp
nhất khi dùng YouTube: để `ffmpeg` tự fetch trực tiếp URL mà yt-dlp
extract ra sẽ bị chặn vì thiếu header/cookie đúng chuẩn mà CDN
`googlevideo.com` yêu cầu. **Cách đã sửa**: không tự fetch URL riêng nữa,
để `yt-dlp` tự tải (`-o -`, xuất ra stdout) rồi pipe thẳng sang `ffmpeg`
qua `stdin` — xem hàm `_transcode_and_cache()` trong `main.py`.

**`'uvicorn' is not recognized` (Windows)** → PATH chưa nhận lệnh, dùng
`python -m uvicorn main:app --reload --port 8000` thay vì gọi thẳng
`uvicorn`.

## Lưu ý quan trọng khác

- **Độ trễ lần đầu**: bài chưa cache mất khoảng 30-45s để xử lý xong toàn
  bộ (tuỳ độ dài bài), nhưng byte đầu tiên trả về gần như ngay lập tức
  (~0.01s đo thực tế) — vì là streaming thật, không phải đợi xử lý xong
  mới gửi. Người nghe sẽ nghe nhạc bắt đầu sau vài giây, không phải chờ
  hết toàn bộ thời gian xử lý.
- **Cache**: file mp3 lưu tại volume `yt2mp3_cache`, lần phát sau cùng
  video_id sẽ gần như tức thì. Chưa có cơ chế dọn cache tự động — nên thêm
  cron job xoá file cũ nếu đĩa đầy theo thời gian.
- **Bản quyền**: chỉ nên dùng cho mục đích cá nhân, không public rộng rãi
  hay thương mại hoá.
- **`yt-dlp` cần cập nhật định kỳ** — YouTube hay đổi cơ chế, bản yt-dlp cũ
  dễ lỗi lại 403 hoặc resolve thất bại. Cân nhắc rebuild định kỳ với
  `pip install -U yt-dlp`.

## File trong repo

- `main.py` — service chính (FastAPI)
- `requirements.txt` — dependencies Python
- `Dockerfile` — build image (có cài sẵn ffmpeg)
- `docker-compose.yml` — chạy độc lập, có sẵn dns fix
- `.env.example` — mẫu biến môi trường, copy thành `.env`
- `debug_timing.py` — script debug đo riêng thời gian resolve vs transcode,
  không phải một phần của service chính, giữ lại để tiện chẩn đoán sau này
  nếu lại chậm bất thường
- `test_device_auth.py` — sinh header xác thực hợp lệ để test `/stream_pcm`
  bằng curl, không cần có thiết bị ESP32 thật trong tay
