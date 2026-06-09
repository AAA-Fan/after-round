#!/usr/bin/env python3
"""Run a command-line mock interview from a generated mock plan."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


DEFAULT_TRANSCRIPT_JSON = Path("data/exports/mock-reports/latest-transcript.json")
DEFAULT_TRANSCRIPT_MD = Path("data/exports/mock-reports/latest-transcript.md")

AnswerProvider = Callable[[str], str]
OutputWriter = Callable[[str], None]
VoiceAnswerProvider = Callable[[str, str, int], dict[str, str]]


def default_answer_provider(prompt: str) -> str:
    return input(prompt)


def build_ffmpeg_record_command(
    audio_path: str | Path,
    *,
    ffmpeg_input: str | None = None,
    ffmpeg_bin: str = "ffmpeg",
) -> list[str]:
    system = platform.system().lower()
    path = str(audio_path)
    if system == "darwin":
        return [
            ffmpeg_bin,
            "-y",
            "-f",
            "avfoundation",
            "-i",
            ffmpeg_input or ":0",
            "-ac",
            "1",
            "-ar",
            "16000",
            path,
        ]
    if system == "linux":
        return [
            ffmpeg_bin,
            "-y",
            "-f",
            "alsa",
            "-i",
            ffmpeg_input or "default",
            "-ac",
            "1",
            "-ar",
            "16000",
            path,
        ]
    return [
        ffmpeg_bin,
        "-y",
        "-i",
        ffmpeg_input or "default",
        "-ac",
        "1",
        "-ar",
        "16000",
        path,
    ]


def stop_ffmpeg(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def transcribe_audio(
    audio_path: str | Path,
    *,
    output_base: str | Path,
    transcribe_script: str | Path | None = None,
) -> str:
    script = Path(transcribe_script) if transcribe_script else Path(__file__).with_name("transcribe_after_round.py")
    command = [
        sys.executable,
        str(script),
        str(audio_path),
        "--output-base",
        str(output_base),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=True)
    txt_path = Path(output_base).with_suffix(".txt")
    if not txt_path.exists():
        raise RuntimeError(f"transcription output not found: {txt_path}\n{completed.stdout}\n{completed.stderr}")
    return txt_path.read_text(encoding="utf-8").strip()


def make_ffmpeg_voice_answer_provider(
    output_dir: str | Path,
    *,
    ffmpeg_input: str | None = None,
    ffmpeg_bin: str = "ffmpeg",
    transcribe_script: str | Path | None = None,
) -> VoiceAnswerProvider:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    def provider(prompt: str, question_id: str, turn_index: int) -> dict[str, str]:
        safe_question = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in question_id)
        output_base = directory / f"{safe_question}-{turn_index:02d}"
        audio_path = output_base.with_suffix(".wav")
        input("按回车开始录音。")
        command = build_ffmpeg_record_command(audio_path, ffmpeg_input=ffmpeg_input, ffmpeg_bin=ffmpeg_bin)
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            input("录音中，回答完后按回车结束。")
        finally:
            stop_ffmpeg(process)
        text = transcribe_audio(audio_path, output_base=output_base, transcribe_script=transcribe_script)
        return {"text": text, "audio_path": str(audio_path)}

    return provider


def answer_turn(
    *,
    prompt: str,
    question_id: str,
    turn_index: int,
    voice_input: bool,
    answer_provider: AnswerProvider,
    voice_answer_provider: VoiceAnswerProvider | None,
) -> dict[str, str | None]:
    if voice_input:
        if voice_answer_provider is None:
            raise ValueError("voice_answer_provider is required when voice_input=True")
        result = voice_answer_provider(prompt, question_id, turn_index)
        return {
            "answer_text": result.get("text", "").strip(),
            "audio_path": result.get("audio_path"),
        }
    return {
        "answer_text": answer_provider("> ").strip(),
        "audio_path": None,
    }


def run_mock_interview(
    plan: dict[str, Any],
    *,
    answer_provider: AnswerProvider = default_answer_provider,
    output: OutputWriter = print,
    voice_input: bool = False,
    voice_answer_provider: VoiceAnswerProvider | None = None,
) -> dict[str, Any]:
    turns: list[dict[str, Any]] = []
    output("# 模拟面试开始")
    output("")
    turn_index = 0
    for question in plan.get("questions", []):
        question_id = str(question["question_id"])
        prompts = [("question", str(question["question"]))]
        prompts.extend(("follow_up", str(item)) for item in question.get("follow_ups", []))
        for prompt_type, prompt in prompts:
            turn_index += 1
            output(f"面试官：{prompt}")
            answer = answer_turn(
                prompt=prompt,
                question_id=question_id,
                turn_index=turn_index,
                voice_input=voice_input,
                answer_provider=answer_provider,
                voice_answer_provider=voice_answer_provider,
            )
            turns.append(
                {
                    "turn_index": turn_index,
                    "question_id": question_id,
                    "prompt_type": prompt_type,
                    "prompt": prompt,
                    "answer_text": answer["answer_text"],
                    "audio_path": answer["audio_path"],
                    "topic": question.get("topic"),
                }
            )
            output("")
    return {
        "plan_id": plan.get("plan_id"),
        "target": plan.get("target", {}),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "voice_input": voice_input,
        "turns": turns,
    }


def render_transcript_markdown(transcript: dict[str, Any]) -> str:
    target = transcript.get("target") or {}
    lines = [
        "# 模拟面试转录",
        "",
        "## 目标",
        "",
        f"- 公司：{target.get('company', '')}",
        f"- 岗位：{target.get('position', '')}",
        f"- 轮次：{target.get('round', '')}",
        "",
        "## 对话",
        "",
    ]
    for turn in transcript.get("turns", []):
        lines.extend(
            [
                f"### Turn {turn['turn_index']} - {turn['topic'] or turn['question_id']}",
                "",
                f"- 面试官：{turn['prompt']}",
                f"- 我：{turn['answer_text']}",
            ]
        )
        if turn.get("audio_path"):
            lines.append(f"- 音频：{turn['audio_path']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_transcript_outputs(
    transcript: dict[str, Any],
    *,
    json_output: str | Path,
    markdown_output: str | Path,
) -> None:
    json_path = Path(json_output)
    md_path = Path(markdown_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_transcript_markdown(transcript), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_json", help="mock_plan.py 生成的 JSON 计划")
    parser.add_argument("--voice-input", action="store_true", help="每题录音后转写")
    parser.add_argument("--voice-output-dir", default="data/exports/mock-reports/audio")
    parser.add_argument("--ffmpeg-input")
    parser.add_argument("--transcript-json", default=str(DEFAULT_TRANSCRIPT_JSON))
    parser.add_argument("--transcript-md", default=str(DEFAULT_TRANSCRIPT_MD))
    args = parser.parse_args(argv)

    plan = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
    voice_provider = None
    if args.voice_input:
        voice_provider = make_ffmpeg_voice_answer_provider(
            args.voice_output_dir,
            ffmpeg_input=args.ffmpeg_input,
        )
    transcript = run_mock_interview(
        plan,
        voice_input=args.voice_input,
        voice_answer_provider=voice_provider,
    )
    write_transcript_outputs(
        transcript,
        json_output=args.transcript_json,
        markdown_output=args.transcript_md,
    )
    print(args.transcript_json)
    print(args.transcript_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
