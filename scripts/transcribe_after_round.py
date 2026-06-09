#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from faster_whisper import WhisperModel


def format_ts(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="Audio or video path to transcribe")
    parser.add_argument("--model", default=os.environ.get("AFTER_ROUND_WHISPER_MODEL", "small"))
    parser.add_argument("--language", default="zh")
    parser.add_argument("--output-base", default=None)
    args = parser.parse_args()

    audio = Path(args.audio).expanduser().resolve()
    if not audio.exists():
        raise FileNotFoundError(audio)

    output_base = Path(args.output_base).expanduser().resolve() if args.output_base else audio.with_suffix("")
    txt_path = output_base.with_suffix(".txt")
    timed_txt_path = output_base.with_name(output_base.name + ".timed.txt")
    json_path = output_base.with_suffix(".segments.json")

    model = WhisperModel(
        args.model,
        device="cpu",
        compute_type="int8",
        cpu_threads=max(1, (os.cpu_count() or 4) - 1),
    )
    segments, info = model.transcribe(
        str(audio),
        language=args.language,
        beam_size=5,
        vad_filter=True,
        initial_prompt="以下是普通话技术面试转录内容，包含项目介绍、算法工程、推荐系统、机器学习、面试官提问和候选人回答：",
    )

    records = []
    with txt_path.open("w", encoding="utf-8") as plain, timed_txt_path.open("w", encoding="utf-8") as timed:
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            record = {
                "start": float(segment.start),
                "end": float(segment.end),
                "start_text": format_ts(segment.start),
                "end_text": format_ts(segment.end),
                "text": text,
            }
            records.append(record)
            plain.write(text + "\n")
            timed.write(f"[{record['start_text']} - {record['end_text']}] {text}\n")
            plain.flush()
            timed.flush()
            print(f"[{record['start_text']} - {record['end_text']}] {text}", flush=True)

    payload = {
        "audio": str(audio),
        "model": args.model,
        "language": args.language,
        "detected_language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "segments": records,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"TXT={txt_path}")
    print(f"TIMED_TXT={timed_txt_path}")
    print(f"JSON={json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
