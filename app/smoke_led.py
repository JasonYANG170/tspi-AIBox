#!/usr/bin/env python3
"""Exercise LED intent routing and PWM state using a temporary sysfs stand-in."""

import json
import tempfile
from collections import deque
from pathlib import Path

import ai


class Display:
    def show(self, *_args):
        pass


class Speech:
    def __init__(self):
        self.spoken = []

    def speak(self, text, cancel=None):
        self.spoken.append(text)


def main():
    examples = {
        "开灯": ("on", None),
        "关灯": ("off", None),
        "把亮度调到百分之五十": ("level", 50),
        "灯调到80%": ("level", 80),
        "灯开到一半": ("level", 50),
        "调亮一点": ("delta", 10),
        "调暗一点": ("delta", -10),
    }
    for utterance, intent in examples.items():
        assert ai.led_intent(utterance) == intent, utterance
    assert ai.led_intent("天亮了") is None
    assert ai.led_intent("为什么关灯后还有微光") is None
    assert ai.led_intent("灯的亮度调节无效，怎么排查") is None

    with tempfile.TemporaryDirectory() as folder:
        channel = Path(folder) / "pwm0"
        channel.mkdir()
        for name in ("period", "duty_cycle", "enable", "polarity"):
            (channel / name).write_text("0" if name == "period" else "")
        state = Path(folder) / "led.json"
        led = ai.LedController(channel=channel, state_file=state)
        assert led.status() == (False, 70)
        assert led.apply("on") == (True, 70)
        assert (channel / "duty_cycle").read_text() == "700000"
        assert led.apply("level", 35) == (True, 35)
        assert (channel / "duty_cycle").read_text() == "350000"
        assert led.apply("off") == (False, 35)
        assert (channel / "duty_cycle").read_text() == "0"
        assert json.loads(state.read_text()) == {"on": False, "brightness": 35}

        display, speech, history = Display(), Speech(), deque(maxlen=6)
        original = ai.respond
        ai.respond = lambda *_args: (_ for _ in ()).throw(AssertionError("LED used LLM"))
        try:
            assert not ai.process_utterance("开灯", history, speech, display, led=led)
            assert not speech.spoken
            assert ai.process_utterance("小嘉，开灯", history, speech, display, led=led)
            assert led.status() == (True, 35)
            assert ai.process_utterance("小佳，亮度调到50%", history, speech, display, led=led)
            assert led.status() == (True, 50)
            assert "百分之50" in speech.spoken[-1]
            assert not history
        finally:
            ai.respond = original
    print("LED speech intent, wake gate, PWM duty, and persistence OK")


if __name__ == "__main__":
    main()
