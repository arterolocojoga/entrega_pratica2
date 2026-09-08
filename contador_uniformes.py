#!/usr/bin/env python3
"""Contador de pessoas com classificação visual de uniforme e alertas IoT."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from ultralytics import YOLO

try:
    import paho.mqtt.client as mqtt
except ImportError:  # MQTT é opcional para permitir execução local sem broker.
    mqtt = None

try:
    import requests
except ImportError:
    requests = None


@dataclass
class Config:
    source: str = "0"
    model_path: str = "yolov8n.pt"
    width: int = 1280
    height: int = 720
    infer_width: int = 640
    process_every: int = 2
    confidence: float = 0.25
    max_match_distance: float = 110.0
    max_missing_frames: int = 12
    min_uniform_ratio: float = 0.12
    # HSV OpenCV para o uniforme verde RGB(121, 242, 178), com tolerância
    # para variações de iluminação e pequenas diferenças de tonalidade.
    uniform_hsv_ranges: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=lambda: [((55, 45, 80), (95, 255, 255))]
    )
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_topic: str = "senai/corredor/alertas"
    webhook_url: str = ""
    alert_cooldown_seconds: int = 20
    log_path: str = "eventos.jsonl"


@dataclass
class Track:
    centroid: tuple[int, int]
    classification: str
    last_seen_frame: int
    box: tuple[int, int, int, int]
    missed_frames: int = 0
    hits: int = 1
    alerted: bool = False


class JsonlLogger:
    def __init__(self, path: str):
        self.path = Path(path)

    def write(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")


class IoTNotifier:
    def __init__(self, config: Config, logger: JsonlLogger):
        self.config = config
        self.logger = logger
        self.client = None
        self.last_alert_at = 0.0
        if config.mqtt_host and mqtt is not None:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="contador-uniformes")
            try:
                self.client.connect(config.mqtt_host, config.mqtt_port, keepalive=30)
                self.client.loop_start()
            except Exception as exc:
                logging.warning("MQTT indisponível: %s", exc)
                self.client = None

    def publish(self, payload: dict[str, Any], alert: bool = False) -> None:
        now = time.time()
        if alert and now - self.last_alert_at < self.config.alert_cooldown_seconds:
            return
        message = json.dumps(payload, ensure_ascii=False)
        sent = False
        if self.client is not None:
            try:
                result = self.client.publish(self.config.mqtt_topic, message, qos=1)
                result.wait_for_publish(timeout=3)
                sent = result.rc == mqtt.MQTT_ERR_SUCCESS
            except Exception as exc:
                logging.warning("Falha ao publicar MQTT: %s", exc)
        if self.config.webhook_url and requests is not None and alert:
            try:
                response = requests.post(self.config.webhook_url, json=payload, timeout=5)
                response.raise_for_status()
                sent = True
            except Exception as exc:
                logging.warning("Falha no webhook de alerta: %s", exc)
        if alert:
            self.last_alert_at = now
        self.logger.write({**payload, "evento": "alerta" if alert else "contagem", "enviado": sent})

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()


def parse_source(value: str) -> str | int:
    return int(value) if value.isdigit() else value


def classify_uniform(frame: np.ndarray, box: tuple[int, int, int, int], config: Config) -> tuple[str, float]:
    x1, y1, x2, y2 = box
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    # Região do tronco: evita rosto e reduz influência do fundo.
    rx1, rx2 = x1 + int(width * 0.18), x2 - int(width * 0.18)
    ry1, ry2 = y1 + int(height * 0.28), y1 + int(height * 0.75)
    crop = frame[max(0, ry1):max(ry1 + 1, ry2), max(0, rx1):max(rx1 + 1, rx2)]
    if crop.size == 0:
        return "sem_uniforme", 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lower, upper in config.uniform_hsv_ranges:
        mask |= cv2.inRange(hsv, np.array(lower), np.array(upper))
    ratio = float(np.count_nonzero(mask)) / float(mask.size)
    return ("com_uniforme" if ratio >= config.min_uniform_ratio else "sem_uniforme", ratio)


def detection_boxes(model: YOLO, frame: np.ndarray, config: Config) -> list[tuple[int, int, int, int]]:
    original_h, original_w = frame.shape[:2]
    scale = config.infer_width / original_w
    small = cv2.resize(frame, (config.infer_width, max(1, int(original_h * scale))))
    results = model(small, verbose=False, conf=config.confidence, classes=[0])
    sx, sy = original_w / small.shape[1], original_h / small.shape[0]
    boxes: list[tuple[int, int, int, int]] = []
    for result in results:
        for box in result.boxes:
            x1, y1, x2, y2 = [int(value) for value in box.xyxy[0].tolist()]
            boxes.append((int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)))
    return boxes


def distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=os.getenv("VIDEO_SOURCE", "0"))
    parser.add_argument("--model", default=os.getenv("YOLO_MODEL", "yolov8n.pt"))
    parser.add_argument("--no-display", action="store_true")
    args = parser.parse_args()
    config = Config(
        source=args.source,
        model_path=args.model,
        mqtt_host=os.getenv("MQTT_HOST", ""),
        mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        mqtt_topic=os.getenv("MQTT_TOPIC", "senai/corredor/alertas"),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL", ""),
        log_path=os.getenv("EVENT_LOG", "eventos.jsonl"),
    )
    logger = JsonlLogger(config.log_path)
    notifier = IoTNotifier(config, logger)
    model = YOLO(config.model_path)
    capture = cv2.VideoCapture(parse_source(config.source))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, config.width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, config.height)
    if not capture.isOpened():
        print(f"Erro: não foi possível abrir a fonte '{config.source}'", file=sys.stderr)
        return 1

    tracks: dict[int, Track] = {}
    next_id = 0
    counts = {"com_uniforme": 0, "sem_uniforme": 0}
    last_day = datetime.now().date()
    frame_number = 0
    boxes_to_draw: list[tuple[tuple[int, int, int, int], str, float]] = []

    print("Contador ativo. Q/ESC encerra; classificação por HSV na região do tronco.")
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                if isinstance(config.source, str):
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                break
            frame_number += 1
            today = datetime.now().date()
            if today != last_day:
                counts = {"com_uniforme": 0, "sem_uniforme": 0}
                tracks.clear()
                last_day = today
                logger.write({"timestamp": datetime.now().isoformat(), "evento": "reset_diario"})

            if frame_number % config.process_every == 0:
                boxes = detection_boxes(model, frame, config)
                current: list[tuple[tuple[int, int], tuple[int, int, int, int], str, float]] = []
                for box in boxes:
                    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
                    classification, ratio = classify_uniform(frame, box, config)
                    current.append(((cx, cy), box, classification, ratio))
                used: set[int] = set()
                # Preserve tracks without detection for a short period. This is
                # essential when YOLO misses one or two frames due to occlusion.
                updated: dict[int, Track] = {}
                for track_id, old in tracks.items():
                    old.missed_frames += 1
                    updated[track_id] = old
                boxes_to_draw = []
                for centroid, box, classification, ratio in current:
                    candidates = []
                    for track_id, old in tracks.items():
                        if track_id in used:
                            continue
                        old_width = max(1, old.box[2] - old.box[0])
                        old_height = max(1, old.box[3] - old.box[1])
                        new_width = max(1, box[2] - box[0])
                        new_height = max(1, box[3] - box[1])
                        adaptive_distance = max(
                            config.max_match_distance,
                            0.45 * max(old_width, old_height, new_width, new_height),
                        )
                        current_distance = distance(centroid, old.centroid)
                        if current_distance <= adaptive_distance:
                            candidates.append((current_distance, track_id, old))
                    if candidates:
                        _, track_id, old = min(candidates, key=lambda item: item[0])
                        used.add(track_id)
                        old.centroid = centroid
                        old.box = box
                        old.classification = classification
                        old.last_seen_frame = frame_number
                        old.missed_frames = 0
                        old.hits += 1
                        track = old
                    else:
                        track_id = next_id
                        next_id += 1
                        track = Track(centroid, classification, frame_number, box)
                        counts[classification] += 1
                        event = {"timestamp": datetime.now().isoformat(), "pessoas_com_uniforme": counts["com_uniforme"], "pessoas_sem_uniforme": counts["sem_uniforme"], "classificacao": classification, "track_id": track_id}
                        notifier.publish(event, alert=classification == "sem_uniforme")
                    updated[track_id] = track
                    boxes_to_draw.append((box, classification, ratio))
                tracks = {
                    track_id: track
                    for track_id, track in updated.items()
                    if track.missed_frames <= config.max_missing_frames
                }

            for (x1, y1, x2, y2), classification, ratio in boxes_to_draw:
                color = (0, 200, 0) if classification == "com_uniforme" else (0, 0, 255)
                label = f"{'UNIFORME' if classification == 'com_uniforme' else 'SEM UNIFORME'} {ratio:.0%}"
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, label, (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

            panel = f"COM UNIFORME: {counts['com_uniforme']}  |  SEM UNIFORME: {counts['sem_uniforme']}"
            cv2.rectangle(frame, (8, 8), (min(frame.shape[1] - 8, 720), 52), (30, 30, 30), -1)
            cv2.putText(frame, panel, (18, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 2)
            if not args.no_display:
                cv2.imshow("Controle de Uniformes - COGNICORE", frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q"), 27):
                    break
    finally:
        capture.release()
        notifier.close()
        if not args.no_display:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
