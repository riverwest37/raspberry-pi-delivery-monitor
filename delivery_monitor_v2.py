#!/usr/bin/env python3
"""Delivery monitor v2: door guard, motion settling, and lighting compensation."""

import argparse
import json
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import cv2
import numpy as np
import paho.mqtt.publish as mqtt_publish
from astral import LocationInfo
from astral.sun import sun

BASE_DIR = Path(__file__).resolve().parent

STREAM_URL = "http://127.0.0.1:8081/?action=stream"

DAY_REFERENCE_FILES = [
    BASE_DIR / "images" / "empty_reference_mat_clear_20261028.jpg",
]

LIGHT_REFERENCE_FILES = [
    BASE_DIR / "images" / "empty_reference_mat_light_clear_20261028.jpg",
    BASE_DIR / "images" / "empty_reference_mat_late_clear_20261028.jpg",
]


# run_onceなど既存の試験処理との互換性を残す
REFERENCE_FILES = DAY_REFERENCE_FILES + LIGHT_REFERENCE_FILES

# Coordinate master: 640 x 360 empty-mat image
COORDINATE_REFERENCE_FILE = (
    BASE_DIR / "images" / "empty_reference_mat_clear_20261028.jpg"
)

# Full artificial-grass mat area defined on the coordinate master
PACKAGE_POLYGON = np.array([
    (350, 150),
    (400, 150),
    (410, 305),
    (345, 305),
], dtype=np.int32)
DOOR_AREA = (0, 80, 300, 280)
LIGHT_AREA = (120, 30, 340, 75)

PACKAGE_THRESHOLD = 3.0
REMOVAL_THRESHOLD = 2.8
DOOR_THRESHOLD = 50.0
MOTION_THRESHOLD = 2.0

STABLE_SECONDS = 5
CONFIRM_SECONDS = 20
RECHECK_WAIT_SECONDS = 60
RECHECK_CONFIRM_SECONDS = 20
CONFIRM_DOOR_LIMIT = 35.0
LIGHT_DRIFT_LIMIT = 10.0
CHECK_INTERVAL = 1.0

# Package detection is active from 07:00 through 17:59.
ACTIVE_START_HOUR = 7
ACTIVE_END_HOUR = 18
LOCAL_TZ = ZoneInfo("Asia/Tokyo")

SUN_LOCATION = LocationInfo(
    "Chiba-Mihama",
    "Japan",
    "Asia/Tokyo",
    35.64,
    140.06,
)

LIGHT_TRANSITION_MINUTES = 10

SAVE_DIR = Path("images/events_v2")
LATEST_IMAGE = Path("images/latest_status_v2.jpg")

MQTT_HOST = "192.168.0.140"
MQTT_PORT = 1883
MQTT_TOPIC = "security/entrance/delivery"

def get_reference_mode(now=None):
    """Return day, transition, or light according to today's sunset."""
    if now is None:
        now = datetime.now(LOCAL_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=LOCAL_TZ)

    solar = sun(
        SUN_LOCATION.observer,
        date=now.date(),
        tzinfo=LOCAL_TZ,
    )

    sunset_time = solar["sunset"]
    light_start = sunset_time + timedelta(
        minutes=LIGHT_TRANSITION_MINUTES
    )

    if now < sunset_time:
        mode = "day"
    elif now < light_start:
        mode = "transition"
    else:
        mode = "light"

    return mode, sunset_time, light_start

def crop(image, area):
    x1, y1, x2, y2 = area
    return image[y1:y2, x1:x2]


def light_level(image):
    roi = crop(image, LIGHT_AREA)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float(gray.mean())


def compensate_brightness(reference, frame):
    """Correct broad brightness drift using an area where packages do not appear."""
    ref_light = max(light_level(reference), 1.0)
    frame_light = max(light_level(frame), 1.0)
    gain = float(np.clip(ref_light / frame_light, 0.55, 1.80))
    corrected = cv2.convertScaleAbs(frame, alpha=gain, beta=0)
    return corrected, gain


def raw_change_percent(reference, frame, area):
    ref_roi = crop(reference, area)
    frame_roi = crop(frame, area)

    ref_gray = cv2.cvtColor(ref_roi, cv2.COLOR_BGR2GRAY)
    frame_gray = cv2.cvtColor(frame_roi, cv2.COLOR_BGR2GRAY)

    ref_gray = cv2.GaussianBlur(ref_gray, (9, 9), 0)
    frame_gray = cv2.GaussianBlur(frame_gray, (9, 9), 0)

    diff = cv2.absdiff(ref_gray, frame_gray)
    _, changed = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)

    kernel = np.ones((5, 5), np.uint8)
    changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, kernel)
    return cv2.countNonZero(changed) / changed.size * 100.0

def masked_change_percent(reference, frame, polygon):
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    ref_gray = cv2.GaussianBlur(ref_gray, (9, 9), 0)
    frame_gray = cv2.GaussianBlur(frame_gray, (9, 9), 0)

    diff = cv2.absdiff(ref_gray, frame_gray)
    _, changed = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)

    kernel = np.ones((5, 5), np.uint8)
    changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, kernel)

    mask = np.zeros(changed.shape, dtype=np.uint8)
    cv2.fillPoly(mask, [polygon], 255)
    changed = cv2.bitwise_and(changed, mask)

    mask_pixels = cv2.countNonZero(mask)
    if mask_pixels == 0:
        return 0.0

    return cv2.countNonZero(changed) / mask_pixels * 100.0


def corrected_masked_change_percent(reference, frame, polygon):
    corrected, gain = compensate_brightness(reference, frame)
    percent = masked_change_percent(reference, corrected, polygon)
    return percent, gain

def corrected_change_percent(reference, frame, area):
    corrected, gain = compensate_brightness(reference, frame)
    percent = raw_change_percent(reference, corrected, area)
    return percent, gain


def frame_motion_percent(previous, current):
    """Detect people/bags still moving, while suppressing overall light drift."""
    corrected, _ = compensate_brightness(previous, current)
    return masked_change_percent(previous, corrected, PACKAGE_POLYGON)

def load_references(filenames=None):
    if filenames is None:
        filenames = REFERENCE_FILES

    references = []
    for filename in filenames:
        image = cv2.imread(filename)
        if image is None:
            print(f"基準画像を読み込めないため除外: {filename}")
            continue
        references.append(
            {
                "filename": filename,
                "image": image,
                "light": light_level(image),
            }
        )

    if not references:
        raise SystemExit("利用できる基準画像がありません")

    return references


def select_reference(references, frame):
    current_light = light_level(frame)
    selected = min(
        references,
        key=lambda ref: abs(ref["light"] - current_light),
    )
    return selected, current_light


def save_event(frame, event_name):
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = SAVE_DIR / f"{event_name}_{timestamp}.jpg"
    cv2.imwrite(str(filename), frame)
    print(f"画像保存: {filename}")
    return filename


def publish_mqtt_event(
    event_name,
    package_change,
    door_change,
    motion_change,
    light,
    reference_name,
    image_filename,
):
    """Publish in a daemon thread so an MQTT outage cannot stop monitoring."""
    payload = {
        "event": event_name,
        "datetime": datetime.now().isoformat(timespec="seconds"),
        "source": "delivery_monitor_v2",
        "package_change": round(package_change, 2),
        "door_change": round(door_change, 2),
        "motion_change": round(motion_change, 2),
        "light": round(light, 1),
        "reference": Path(reference_name).name,
        "image": str(image_filename),
    }

    def worker():
        try:
            mqtt_publish.single(
                MQTT_TOPIC,
                payload=json.dumps(payload, ensure_ascii=False),
                qos=1,
                # Keep the latest parcel state so the Pico W can restore its
                # green LED after a reboot or temporary Wi-Fi interruption.
                retain=True,
                hostname=MQTT_HOST,
                port=MQTT_PORT,
            )
            print(f"MQTT送信: {MQTT_TOPIC} event={event_name}")
        except Exception as error:
            print(f"MQTT送信失敗（監視は継続）: {error}")

    threading.Thread(target=worker, daemon=True).start()


def draw_status(
    frame,
    status,
    package_change,
    door_change,
    motion_change,
    reference_name,
    gain,
):
    result = frame.copy()
    dx1, dy1, dx2, dy2 = DOOR_AREA

    cv2.polylines(result, [PACKAGE_POLYGON], True, (0, 255, 255), 2)
    cv2.rectangle(result, (dx1, dy1), (dx2, dy2), (255, 0, 255), 2)

    color = (0, 255, 0)
    if status == "PACKAGE DETECTED":
        color = (0, 0, 255)
    elif status in ("DOOR/MOTION", "MOVING"):
        color = (0, 165, 255)
    elif status.startswith(("STABILIZING", "CHECKING", "REMOVAL CHECK")):
        color = (0, 255, 255)

    lines = [
        status,
        f"Package:{package_change:.1f}% Door:{door_change:.1f}% Motion:{motion_change:.1f}%",
        f"Ref:{Path(reference_name).name} Gain:{gain:.2f}",
    ]
    for index, text in enumerate(lines):
        cv2.putText(
            result,
            text,
            (15, 30 + index * 27),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.56 if index else 0.70,
            color if index == 0 else (255, 255, 255),
            2,
        )

    cv2.imwrite(str(LATEST_IMAGE), result)


def evaluate_frame(references, frame):
    selected, current_light = select_reference(references, frame)
    reference = selected["image"]
    # Artificial-grass mat is compared without global brightness correction.
    # The tiled LIGHT_AREA and the green mat respond differently to sunlight.
    package_change = masked_change_percent(
        reference, frame, PACKAGE_POLYGON
    )
    _, gain = compensate_brightness(reference, frame)

    door_change, _ = corrected_change_percent(reference, frame, DOOR_AREA)
    return {
        "selected": selected,
        "current_light": current_light,
        "package_change": package_change,
        "door_change": door_change,
        "gain": gain,
    }


def run_once(filename):
    references = load_references()
    frame = cv2.imread(filename)
    if frame is None:
        raise SystemExit(f"画像を読み込めません: {filename}")

    result = evaluate_frame(references, frame)
    ref = result["selected"]
    print(f"試験画像: {filename}")
    print(f"選択基準: {ref['filename']}")
    print(
        f"照度: 現在={result['current_light']:.1f} "
        f"基準={ref['light']:.1f} 補正倍率={result['gain']:.2f}"
    )
    print(f"荷物変化率: {result['package_change']:.2f}%")
    print(f"扉変化率: {result['door_change']:.2f}%")

def run_monitor():
    day_references = load_references(DAY_REFERENCE_FILES)
    light_references = load_references(LIGHT_REFERENCE_FILES)
    references = day_references + light_references

    cap = cv2.VideoCapture(STREAM_URL)
    if not cap.isOpened():
        raise SystemExit("カメラ映像を開けません")

    package_present = False
    stable_since = None
    candidate_since = None
    candidate_light = None
    recheck_due = None
    recheck_since = None
    recheck_light = None
    removal_since = None
    removal_light = None
    removal_recheck_due = None
    removal_recheck_since = None
    removal_recheck_light = None
    previous_frame = None
    previous_status = None
    previous_reference_mode = None
    last_check = 0.0

    print("置き配監視V2を開始しました")
    print(
        f"静止確認={STABLE_SECONDS}秒 "
        f"仮判定={CONFIRM_SECONDS}秒 "
        f"再判定待機={RECHECK_WAIT_SECONDS}秒 "
        f"再確認={RECHECK_CONFIRM_SECONDS}秒 "
        f"撤去しきい値={REMOVAL_THRESHOLD:.0f}% "
        f"扉しきい値={DOOR_THRESHOLD:.0f}%"
    )
    print(
        f"監視時間={ACTIVE_START_HOUR:02d}:00-"
        f"{ACTIVE_END_HOUR:02d}:00（時間外はNIGHT PAUSE）"
    )
    for ref in references:
        print(f"基準画像: {ref['filename']} 照度={ref['light']:.1f}")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("映像取得失敗。3秒後に再接続します")
                cap.release()
                time.sleep(3)
                cap = cv2.VideoCapture(STREAM_URL)
                previous_frame = None
                continue

            now = time.monotonic()
            if now - last_check < CHECK_INTERVAL:
                continue
            last_check = now

            ref_shape = references[0]["image"].shape[:2]
            if frame.shape[:2] != ref_shape:
                frame = cv2.resize(frame, (ref_shape[1], ref_shape[0]))

            current_hour = datetime.now().hour
            if not (ACTIVE_START_HOUR <= current_hour < ACTIVE_END_HOUR):
                stable_since = None
                candidate_since = None
                candidate_light = None
                recheck_due = None
                recheck_since = None
                recheck_light = None
                removal_since = None
                removal_light = None
                removal_recheck_due = None
                removal_recheck_since = None
                removal_recheck_light = None
                previous_frame = None
                previous_reference_mode = None
                status = "NIGHT PAUSE"

                draw_status(
                    frame,
                    status,
                    0.0,
                    0.0,
                    0.0,
                    f"schedule_{ACTIVE_START_HOUR:02d}-{ACTIVE_END_HOUR:02d}",
                    1.0,
                )

                if status != previous_status:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"{timestamp} {status} "
                        f"監視時間={ACTIVE_START_HOUR:02d}:00-"
                        f"{ACTIVE_END_HOUR:02d}:00"
                    )
                    previous_status = status

                time.sleep(CHECK_INTERVAL)
                continue

            current_time = datetime.now(LOCAL_TZ)
            reference_mode, sunset_time, light_start = (
                get_reference_mode(current_time)
            )

            # 日没直後は玄関ライトの点灯・明るさ変化が安定するまで休止
            if reference_mode == "transition":
                stable_since = None
                candidate_since = None
                candidate_light = None
                recheck_due = None
                recheck_since = None
                recheck_light = None
                removal_since = None
                removal_light = None
                removal_recheck_due = None
                removal_recheck_since = None
                removal_recheck_light = None
                previous_frame = None
                previous_reference_mode = reference_mode
                status = "LIGHT TRANSITION"

                draw_status(
                    frame,
                    status,
                    0.0,
                    0.0,
                    0.0,
                    f"sunset_{sunset_time.strftime('%H%M')}",
                    1.0,
                )

                if status != previous_status:
                    timestamp = current_time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    print(
                        f"{timestamp} {status} "
                        f"日没={sunset_time.strftime('%H:%M:%S')} "
                        f"ライト基準開始="
                        f"{light_start.strftime('%H:%M:%S')}"
                    )
                    previous_status = status

                time.sleep(CHECK_INTERVAL)
                continue

            if reference_mode == "day":
                active_references = day_references
            else:
                active_references = light_references

            # 昼間基準とライト基準が切り替わる際は判定を初期化
            if reference_mode != previous_reference_mode:
                stable_since = None
                candidate_since = None
                candidate_light = None
                recheck_due = None
                recheck_since = None
                recheck_light = None
                removal_since = None
                removal_light = None
                removal_recheck_due = None
                removal_recheck_since = None
                removal_recheck_light = None
                previous_frame = None

                timestamp = current_time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                print(
                    f"{timestamp} REFERENCE MODE "
                    f"{reference_mode.upper()} "
                    f"日没={sunset_time.strftime('%H:%M:%S')}"
                )
                previous_reference_mode = reference_mode

            result = evaluate_frame(active_references, frame)


            package_change = result["package_change"]
            door_change = result["door_change"]

            if previous_frame is None:
                motion_change = 0.0
            else:
                motion_change = frame_motion_percent(previous_frame, frame)
            previous_frame = frame.copy()

            if door_change >= DOOR_THRESHOLD:
                stable_since = None
                candidate_since = None
                candidate_light = None
                recheck_due = None
                recheck_since = None
                recheck_light = None
                removal_since = None
                removal_light = None
                removal_recheck_due = None
                removal_recheck_since = None
                removal_recheck_light = None
                status = "DOOR/MOTION"

            elif motion_change >= MOTION_THRESHOLD:
                stable_since = None
                candidate_since = None
                candidate_light = None
                recheck_due = None
                recheck_since = None
                recheck_light = None
                removal_since = None
                removal_light = None
                removal_recheck_due = None
                removal_recheck_since = None
                removal_recheck_light = None
                status = "MOVING"

            else:
                if stable_since is None:
                    stable_since = now
                stable_elapsed = int(now - stable_since)

                if stable_elapsed < STABLE_SECONDS:
                    candidate_since = None
                    candidate_light = None
                    removal_since = None
                    removal_light = None
                    removal_recheck_due = None
                    removal_recheck_since = None
                    removal_recheck_light = None
                    status = f"STABILIZING {stable_elapsed}/{STABLE_SECONDS}s"

                elif not package_present:
                    if door_change >= CONFIRM_DOOR_LIMIT:
                        stable_since = None
                        candidate_since = None
                        candidate_light = None
                        recheck_due = None
                        recheck_since = None
                        recheck_light = None
                        status = "BROAD CHANGE"

                    elif recheck_due is not None:
                        if now < recheck_due:
                            remaining = int(recheck_due - now) + 1
                            status = f"RECHECK WAIT {remaining}s"
                        elif package_change < PACKAGE_THRESHOLD:
                            recheck_due = None
                            recheck_since = None
                            recheck_light = None
                            status = "NO PACKAGE"
                        else:
                            if recheck_since is None:
                                recheck_since = now
                                recheck_light = result["current_light"]
                            light_drift = abs(
                                result["current_light"] - recheck_light
                            )
                            if light_drift > LIGHT_DRIFT_LIMIT:
                                recheck_since = None
                                recheck_light = None
                                status = f"RECHECK LIGHT CHANGE {light_drift:.1f}"
                            else:
                                elapsed = int(now - recheck_since)
                                if elapsed >= RECHECK_CONFIRM_SECONDS:
                                    package_present = True
                                    recheck_due = None
                                    recheck_since = None
                                    recheck_light = None
                                    status = "PACKAGE DETECTED"
                                    event_image = save_event(
                                        frame, "package_detected"
                                    )
                                    publish_mqtt_event(
                                        "package_detected",
                                        package_change,
                                        door_change,
                                        motion_change,
                                        result["current_light"],
                                        result["selected"]["filename"],
                                        event_image,
                                    )
                                else:
                                    status = (
                                        f"RECHECKING {elapsed}/"
                                        f"{RECHECK_CONFIRM_SECONDS}s"
                                    )

                    elif package_change >= PACKAGE_THRESHOLD:
                        if candidate_since is None:
                            candidate_since = now
                            candidate_light = result["current_light"]
                        light_drift = abs(
                            result["current_light"] - candidate_light
                        )
                        if light_drift > LIGHT_DRIFT_LIMIT:
                            candidate_since = None
                            candidate_light = None
                            status = f"LIGHT CHANGE {light_drift:.1f}"
                        else:
                            elapsed = int(now - candidate_since)
                            if elapsed >= CONFIRM_SECONDS:
                                candidate_since = None
                                candidate_light = None
                                recheck_due = now + RECHECK_WAIT_SECONDS
                                status = (
                                    f"PROVISIONAL RECHECK IN "
                                    f"{RECHECK_WAIT_SECONDS}s"
                                )
                            else:
                                status = f"CHECKING {elapsed}/{CONFIRM_SECONDS}s"
                    else:
                        candidate_since = None
                        candidate_light = None
                        status = "NO PACKAGE"

                else:
                    if removal_recheck_due is not None:
                        if now < removal_recheck_due:
                            remaining = int(removal_recheck_due - now) + 1
                            status = f"REMOVAL RECHECK WAIT {remaining}s"
                        elif package_change >= REMOVAL_THRESHOLD:
                            removal_since = None
                            removal_light = None
                            removal_recheck_due = None
                            removal_recheck_since = None
                            removal_recheck_light = None
                            status = "PACKAGE DETECTED"
                        else:
                            if removal_recheck_since is None:
                                removal_recheck_since = now
                                removal_recheck_light = result["current_light"]
                            light_drift = abs(
                                result["current_light"] - removal_recheck_light
                            )
                            if light_drift > LIGHT_DRIFT_LIMIT:
                                removal_recheck_since = None
                                removal_recheck_light = None
                                status = (
                                    f"REMOVAL RECHECK LIGHT CHANGE "
                                    f"{light_drift:.1f}"
                                )
                            else:
                                elapsed = int(now - removal_recheck_since)
                                if elapsed >= RECHECK_CONFIRM_SECONDS:
                                    package_present = False
                                    removal_since = None
                                    removal_light = None
                                    removal_recheck_due = None
                                    removal_recheck_since = None
                                    removal_recheck_light = None
                                    status = "PACKAGE REMOVED"
                                    event_image = save_event(
                                        frame, "package_removed"
                                    )
                                    publish_mqtt_event(
                                        "package_removed",
                                        package_change,
                                        door_change,
                                        motion_change,
                                        result["current_light"],
                                        result["selected"]["filename"],
                                        event_image,
                                    )
                                else:
                                    status = (
                                        f"REMOVAL RECHECKING {elapsed}/"
                                        f"{RECHECK_CONFIRM_SECONDS}s"
                                    )

                    elif package_change < REMOVAL_THRESHOLD:
                        if removal_since is None:
                            removal_since = now
                            removal_light = result["current_light"]
                        light_drift = abs(
                            result["current_light"] - removal_light
                        )
                        if light_drift > LIGHT_DRIFT_LIMIT:
                            removal_since = None
                            removal_light = None
                            status = f"REMOVAL LIGHT CHANGE {light_drift:.1f}"
                        else:
                            elapsed = int(now - removal_since)
                            if elapsed >= CONFIRM_SECONDS:
                                removal_since = None
                                removal_light = None
                                removal_recheck_due = now + RECHECK_WAIT_SECONDS
                                status = (
                                    f"PROVISIONAL REMOVAL RECHECK IN "
                                    f"{RECHECK_WAIT_SECONDS}s"
                                )
                            else:
                                status = (
                                    f"REMOVAL CHECK {elapsed}/"
                                    f"{CONFIRM_SECONDS}s"
                                )
                    else:
                        removal_since = None
                        removal_light = None
                        removal_recheck_due = None
                        removal_recheck_since = None
                        removal_recheck_light = None
                        status = "PACKAGE DETECTED"

            draw_status(
                frame,
                status,
                package_change,
                door_change,
                motion_change,
                result["selected"]["filename"],
                result["gain"],
            )

            if status != previous_status:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"{timestamp} {status} "
                    f"荷物={package_change:.2f}% "
                    f"扉={door_change:.2f}% "
                    f"動き={motion_change:.2f}% "
                    f"照度={result['current_light']:.1f} "
                    f"基準={Path(result['selected']['filename']).name}"
                )
                previous_status = status

    except KeyboardInterrupt:
        print("\n置き配監視V2を終了します")
    finally:
        cap.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        metavar="IMAGE",
        help="保存画像1枚をV2条件で評価して終了",
    )
    args = parser.parse_args()

    if args.once:
        run_once(args.once)
    else:
        run_monitor()


if __name__ == "__main__":
    main()
