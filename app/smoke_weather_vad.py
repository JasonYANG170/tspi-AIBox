#!/usr/bin/env python3
"""Check adaptive voice trigger and local weather routing on the board."""

from collections import deque

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
    quiet = ai.AmbientGate()
    assert not any(quiet.observe(0.004) for _ in range(10))
    assert quiet.observe(0.025)
    assert quiet.end_threshold < quiet.threshold

    noisy = ai.AmbientGate()
    assert not any(noisy.observe(0.018) for _ in range(10))
    assert noisy.threshold > quiet.threshold
    assert not noisy.observe(0.025)
    assert noisy.observe(0.055)

    assert ai.local_weather_answer("温度传感器为什么读数不准") is None
    assert ai.local_weather_answer("湿度异常如何排查") is None

    display, tts, history = Display(), TTS(), deque(maxlen=6)
    original = ai.respond
    original_read = ai.read_aht10
    ai.respond = lambda *_args: (_ for _ in ()).throw(AssertionError("weather used LLM"))
    ai.read_aht10 = lambda: {"temperature_c": 24.5, "humidity_percent": 51.0}
    try:
        assert not ai.process_utterance("今天天气怎么样", history, tts, display)
        assert not tts.spoken
        assert ai.process_utterance("小佳，今天天气怎么样", history, tts, display)
        assert ai.process_utterance("小佳，今天天气如何", history, tts, display)
        assert len(tts.spoken) == 2
        assert "温度" in tts.spoken[0] and "湿度" in tts.spoken[0]
        assert "预报" in tts.spoken[0]
        assert not history
    finally:
        ai.respond = original
        ai.read_aht10 = original_read
    print("adaptive VAD, wake gate, and weather routing OK")


if __name__ == "__main__":
    main()
