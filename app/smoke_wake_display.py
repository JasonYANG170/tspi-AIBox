#!/usr/bin/env python3
"""Verify wake filtering and changing OLED frames without audio hardware."""

from collections import deque
from pathlib import Path

from PIL import ImageFont

import ai


class Display:
    def __init__(self):
        self.states = []

    def show(self, text, mood=None):
        self.states.append((text, mood))


class TTS:
    def __init__(self):
        self.spoken = []

    def speak(self, text, cancel=None):
        self.spoken.append(text)


def main():
    for text in ("小嘉，你是谁", "你好小佳", "晓嘉，今天天气如何", "小家，介绍一下自己"):
        assert ai.wake_question(text) is not None, text
    for text in ("今天天气如何", "小家伙真可爱", "请介绍一下自己"):
        assert ai.wake_question(text) is None, text

    display, tts, history = Display(), TTS(), deque(maxlen=6)
    original = ai.respond
    calls = []

    def fake_respond(question, _history, _tts, _display, _cancel=None):
        calls.append(question)
        return "回答"

    ai.respond = fake_respond
    try:
        assert not ai.process_utterance("今天天气如何", history, tts, display)
        assert not calls and not tts.spoken
        assert ai.process_utterance("你好，小佳，你是谁？", history, tts, display)
        assert calls == ["你好，你是谁"]
        assert ai.process_utterance("小嘉", history, tts, display)
        assert tts.spoken == ["我在，请说。"]
    finally:
        ai.respond = original

    path = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    font = ImageFont.truetype(str(path), 14) if path.exists() else ImageFont.load_default()
    for mood in ("idle", "listening", "thinking", "speaking", "happy", "sad", "curious", "sleepy"):
        frame = ai.render_oled_frame("等待唤醒：小嘉", mood, 3, font)
        assert frame.size == (128, 64) and frame.getbbox(), mood
    idle_a = ai.render_oled_frame("等待唤醒：小嘉", "idle", 0, font)
    idle_b = ai.render_oled_frame("等待唤醒：小嘉", "idle", 3, font)
    assert idle_a.tobytes() != idle_b.tobytes(), "idle face does not blink"
    print("wake filter, silence, acknowledgement, and animated moods OK")


if __name__ == "__main__":
    main()
