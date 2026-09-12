#!/usr/bin/env python3
"""新しい置き配検出写真をDropboxへ保存し、MQTTで保存完了を通知する。"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.publish as publish


BASE_DIR = Path(__file__).resolve().parent
PHOTO_DIR = BASE_DIR / "images" / "events_v2"
STATE_FILE = BASE_DIR / ".uploaded_delivery_photos.json"

FILE_PATTERN = "package_detected_*.jpg"
DROPBOX_BASE = "dropbox:置き配監視システム/配達写真"

MQTT_HOST = "192.168.0.140"
MQTT_PORT = 1883
MQTT_TOPIC = "security/entrance/delivery/photo"

POLL_SECONDS = 5


def load_processed():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data)
    except (OSError, ValueError, TypeError):
        return set()


def save_processed(processed):
    temporary = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    temporary.write_text(
        json.dumps(sorted(processed), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, STATE_FILE)


def upload_photo(photo):
    date_folder = datetime.fromtimestamp(photo.stat().st_mtime).strftime("%Y-%m-%d")
    dropbox_path = f"{DROPBOX_BASE}/{date_folder}/{photo.name}"

    subprocess.run(
        [
            "rclone",
            "copyto",
            str(photo),
            dropbox_path,
            "--retries",
            "3",
        ],
        check=True,
        timeout=120,
    )

    payload = {
        "event": "photo_saved",
        "datetime": datetime.now().isoformat(timespec="seconds"),
        "filename": photo.name,
        "dropbox_path": f"置き配監視システム／配達写真／{date_folder}",
    }

    publish.single(
        MQTT_TOPIC,
        payload=json.dumps(payload, ensure_ascii=False),
        qos=1,
        retain=False,
        hostname=MQTT_HOST,
        port=MQTT_PORT,
    )

    logging.info("Dropbox保存・MQTT通知成功: %s", photo.name)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    processed = load_processed()

    if not STATE_FILE.exists():
        processed.update(photo.name for photo in PHOTO_DIR.glob(FILE_PATTERN))
        save_processed(processed)
        logging.info("既存写真 %d 件を登録（転送なし）", len(processed))

    logging.info("写真自動転送を開始: %s", PHOTO_DIR)

    while True:
        for photo in sorted(PHOTO_DIR.glob(FILE_PATTERN)):
            if photo.name in processed:
                continue

            try:
                upload_photo(photo)
                processed.add(photo.name)
                save_processed(processed)
            except Exception:
                logging.exception("写真転送失敗、次回再試行: %s", photo.name)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
