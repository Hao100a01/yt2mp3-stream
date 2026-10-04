"""
Script test thủ công: mô phỏng chính xác cách firmware ESP32 tính
X-Dynamic-Key, để test /stream_pcm và /audio/*.mp3 bằng curl mà không cần
có thiết bị thật trong tay.

Dùng:
    python3 test_device_auth.py [song] [artist]

In ra lệnh curl đầy đủ, copy chạy thử trực tiếp.
"""
import hashlib
import sys
import time

# PHẢI khớp đúng ESP32_SECRET_KEY trong main.py / biến môi trường
SECRET_KEY = "your-esp32-secret-key-2024"

# Giá trị giả lập -- khi test với thiết bị thật, dùng đúng giá trị MAC/Chip
# ID thật của board đó (firmware tự gửi, không cần tự bịa).
MAC = "AA:BB:CC:DD:EE:FF"
CHIP_ID = "1234567890"
TIMESTAMP = str(int(time.time()))  # giả lập, firmware dùng giây từ lúc boot

key_material = f"{MAC}:{CHIP_ID}:{TIMESTAMP}:{SECRET_KEY}"
digest = hashlib.sha256(key_material.encode()).digest()[:16]
dynamic_key = digest.hex().upper()

song = sys.argv[1] if len(sys.argv) > 1 else "Ech Ngoai Day Gieng"
artist = sys.argv[2] if len(sys.argv) > 2 else ""

base_url = "http://localhost:8000"

print("=== Headers sẽ dùng ===")
print(f"X-MAC-Address: {MAC}")
print(f"X-Chip-ID: {CHIP_ID}")
print(f"X-Timestamp: {TIMESTAMP}")
print(f"X-Dynamic-Key: {dynamic_key}")
print()
print("=== Lệnh curl test /stream_pcm ===")
print(
    f'curl -s -G "{base_url}/stream_pcm" '
    f'--data-urlencode "song={song}" '
    f'--data-urlencode "artist={artist}" '
    f'-H "X-MAC-Address: {MAC}" '
    f'-H "X-Chip-ID: {CHIP_ID}" '
    f'-H "X-Timestamp: {TIMESTAMP}" '
    f'-H "X-Dynamic-Key: {dynamic_key}"'
)
print()
print("=== Test không có header (phải bị từ chối 401) ===")
print(f'curl -s -i -G "{base_url}/stream_pcm" --data-urlencode "song={song}"')
