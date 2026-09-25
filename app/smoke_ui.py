#!/usr/bin/env python3
"""Check screen layout, button navigation, and conversation modes without GPIO presses."""

import json
import tempfile
import threading
from collections import deque
from pathlib import Path

from PIL import ImageFont

import ai


class Speech:
    def __init__(self):
        self.spoken = []

    def speak(self, text, cancel=None):
        self.spoken.append(text)


class Led:
    def __init__(self):
        self.on, self.level = False, 70

    def status(self):
        return self.on, self.level

    def apply(self, command, value):
        assert command == "level"
        self.level, self.on = value, value > 0
        return self.status()


def main():
    font_path = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    font = ImageFont.truetype(str(font_path), 14) if font_path.exists() else ImageFont.load_default()
    for mode in ai.DISPLAY_MODES:
        image = ai.render_oled_frame("小嘉准备好了", "idle", 3, font, mode)
        assert image.size == (128, 64) and image.getbbox(), mode
    weather = {"temperature_c": 24.5, "humidity_percent": 58.3}
    for page in ("weather", "settings"):
        image = ai.render_menu_frame(page, 0, "face_text", "wake", weather, font)
        assert image.size == (128, 64) and image.getbbox(), page
    last_item = ai.render_menu_frame("settings", 5, "face_text", "wake", weather,
                                     font, prompt_enabled=False)
    assert last_item.size == (128, 64) and last_item.getbbox()
    assert ai.llm_messages("你好", [], True)[0]["role"] == "system"
    assert ai.llm_messages("你好", [], False) == [{"role": "user", "content": "你好"}]

    with tempfile.TemporaryDirectory() as folder:
        original = ai.SETTINGS_FILE
        ai.SETTINGS_FILE = Path(folder) / "settings.json"
        display = ai.Display.__new__(ai.Display)
        display.lock = threading.Lock()
        display.led = Led()
        display.page = "face"
        display.menu_index = 0
        display.display_mode = "face_text"
        display.conversation_mode = "wake"
        display.prompt_enabled = True
        display.llm_model = ai.MODELS[1]
        display.answer_cancel = None
        display.interrupt_playback = None
        display.button_armed = False
        display.changed = 0.0
        display.text = ""
        display.mood = "idle"
        display.weather_at = 0.0
        try:
            display.handle_button(3)
            assert display.page == "weather"
            display.handle_button(3)
            assert display.page == "settings"
            display.handle_button(2)
            assert display.display_mode == "text"
            display.handle_button(3)
            display.handle_button(2)
            assert display.mode() == "continuous"
            assert display.idle_text() == "连续聆听中"
            speech, history = Speech(), deque(maxlen=6)
            original_respond = ai.respond
            ai.respond = lambda question, *_args: question
            try:
                assert ai.process_utterance("介绍一下自己", history, speech, display,
                                            allow_without_wake=True)
                assert not ai.process_utterance("介绍一下自己", history, speech, display)
            finally:
                ai.respond = original_respond
            display.handle_button(2)
            assert display.mode() == "button"
            display.handle_button(3)
            assert display.menu_index == 2
            display.handle_button(2)
            assert display.led.status() == (True, 25)
            display.handle_button(3)
            display.handle_button(2)
            assert not display.system_prompt_enabled()
            assert json.loads(ai.SETTINGS_FILE.read_text())["prompt_enabled"] is False
            display.handle_button(3)
            assert display.menu_index == 4
            display.handle_button(2)
            assert display.selected_model() == ai.MODELS[0]
            assert json.loads(ai.SETTINGS_FILE.read_text())["llm_model"] == ai.MODELS[0]
            display.handle_button(3)
            assert display.menu_index == 5
            display.handle_button(2)
            assert display.page == "face"
            display.handle_button(2)
            assert display.take_button_arm()
            assert not display.take_button_arm()
            assert json.loads(ai.SETTINGS_FILE.read_text())["conversation_mode"] == "button"

        finally:
            ai.SETTINGS_FILE = original
    print("OLED modes, button navigation, persistence, and dialogue modes OK")


if __name__ == "__main__":
    main()
