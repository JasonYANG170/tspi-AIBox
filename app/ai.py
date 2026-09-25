#!/usr/bin/env python3
"""Offline RK3566 voice assistant: RKNPU ASR, local Ollama, local TTS."""

import json
import logging
import os
import queue
import re
import select
import signal
import subprocess
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np
import sherpa_onnx

BASE = Path(os.getenv("EDA_AI_ROOT", "/opt/eda-aibox"))
NPU = BASE / "models/sherpa-onnx-rk3566-streaming-zipformer-small-bilingual-zh-en-2023-02-16"
SENSE = BASE / "models/sherpa-onnx-rk3566-5-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17"
CPU = BASE / "models/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16"
VOICE = BASE / "models/vits-icefall-zh-aishell3"
VOICE_XIAO = BASE / "models/vits-piper-zh_CN-xiao_ya-medium"
MODEL = os.getenv("EDA_LLM_MODEL", "qwen3:1.7b-q4_K_M")
MODELS = ("qwen3:0.6b", "qwen3:1.7b-q4_K_M")
MIC = os.getenv("EDA_CAPTURE_DEVICE", "plughw:1,0")
SPEAKER = os.getenv("EDA_PLAYBACK_DEVICE", "plughw:1,0")
LOG = logging.getLogger("eda-aibox")
STOP = threading.Event()
AHT_LOCK = threading.Lock()
SETTINGS_FILE = Path(os.getenv("EDA_SETTINGS_FILE", "/var/lib/eda-aibox/settings.json"))
LED_STATE_FILE = Path(os.getenv("EDA_LED_STATE_FILE", "/var/lib/eda-aibox/led.json"))
PWM_PERIOD_NS = 1_000_000
DISPLAY_MODES = ("face", "face_text", "text")
CONVERSATION_MODES = ("wake", "continuous", "button")
SETTINGS_ITEMS = ("display", "conversation", "led", "prompt", "model", "return")
PROMPT = (
    "你是泰山派上的离线语音助手小嘉。你能离线对话、讲故事、解释知识、"
    "提供电子设计和故障排查建议；板上有AHT10温湿度传感器，"
    "GPIO0_B7通过PWM0控制LED亮度。"
    "优先完成用户当前的请求：问问题就直接回答，"
    "请求故事就开始讲故事，请求解决故障就按可能性给出具体检查步骤、"
    "预期现象和下一步判断。"
    "历史对话仅用于理解当前问题，不要沿用上轮的拒答或错误结论。"
    "先在合理假设下提供帮助，只有缺少关键条件时才简短追问；不要无故拒答、"
    "说空话或反复让用户重新提问。回答要适合朗读，使用自然中文，不用 Markdown。"
    "普通问题回答两到四句，复杂问题可以多说几句。无法获取实时信息时如实说明，"
    "不要编造传感器数据。要求很长的内容时先给第一段，让用户可以继续追问。"
)
WAKE_WORD = re.compile(
    r"[小晓筱][\s，,。！？?!、]*(?:[嘉佳加甲价架驾]|家(?!伙|庭|电|具|务|子))"
)


def shutdown(_signum, _frame):
    STOP.set()


def wake_question(text):
    """Return the request after a homophonic wake name, or None if not addressed."""
    match = WAKE_WORD.search(text)
    if match is None:
        return None
    question = text[:match.start()] + text[match.end():]
    question = re.sub(r"[，,。！？?!、：:]{2,}", "，", question)
    return question.strip(" \t，,。！？?!、：:")


WEATHER_WORD = re.compile(r"天气|气温|温度|湿度|室温|几度|冷不冷|热不热")
WEATHER_EXPLANATION = re.compile(r"为什么|怎么(?!样)|原因|原理|故障|排查|校准|不准|异常|解决|维修|修复|代码|编程|区别")


def local_weather_answer(question):
    """Answer weather questions with the sensor reading, never a forecast."""
    if not WEATHER_WORD.search(question) or WEATHER_EXPLANATION.search(question):
        return None
    try:
        reading = read_aht10()
        LOG.info("AHT10 temperature=%.2f humidity=%.2f", reading["temperature_c"], reading["humidity_percent"])
        return (f"板子附近温度{reading['temperature_c']:.1f}摄氏度，"
                f"相对湿度{reading['humidity_percent']:.1f}%。"
                "这是本地传感器读数，不代表室外天气或预报。")
    except Exception:
        LOG.exception("AHT10 reading failed")
        return "现在无法读取板子附近的温湿度，请稍后再试。"


def read_aht10():
    from smbus2 import SMBus
    from aht10_read import measure

    bus_number = int(os.getenv("EDA_AHT_BUS", "2"))
    address = int(os.getenv("EDA_AHT_ADDRESS", "0x38"), 0)
    with AHT_LOCK, SMBus(bus_number) as bus:
        return measure(bus, address)


def chinese_number(value):
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value.isdigit():
        return int(value)
    if value == "百":
        return 100
    if "百" in value:
        left, right = value.split("百", 1)
        return digits.get(left, 1) * 100 + chinese_number(right or "零")
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(value)


def led_intent(text):
    """Return a small, explicit LED command without invoking the LLM."""
    if not re.search(r"灯|亮度|调亮|调暗", text):
        return None
    if re.search(r"为什么|怎么|如何|怎样|原因|原理|故障|排查|不准|异常|解决|维修|修复|代码|编程", text):
        return None
    if re.search(r"关灯|关闭.{0,3}灯|灯.{0,3}关|熄灯|熄灭.{0,3}灯", text):
        return ("off", None)
    if "一半" in text:
        return ("level", 50)
    if "最亮" in text:
        return ("level", 100)
    if "最暗" in text:
        return ("level", 5)
    if re.search(r"亮度|调到|设为|设置为|开到|灯到", text):
        match = re.search(r"(?:百分之)?\s*(\d{1,3}|[零一二两三四五六七八九十百]{1,5})\s*(?:%|％)?", text)
        if match:
            level = chinese_number(match.group(1))
            if level is not None and 0 <= level <= 100:
                return ("level", level)
    if re.search(r"调亮|亮一点|更亮", text):
        return ("delta", 10)
    if re.search(r"调暗|暗一点|更暗", text):
        return ("delta", -10)
    if re.search(r"开灯|打开.{0,3}灯|点亮.{0,3}灯|灯.{0,3}开", text):
        return ("on", None)
    if re.search(r"亮度|灯.*(?:多亮|多少)", text) and re.search(r"多少|几", text):
        return ("query", None)
    return None


class LedController:
    """Control GPIO0_B7 through PWM0 sysfs; the kernel generates the waveform."""

    def __init__(self, channel=None, state_file=None):
        self.lock = threading.Lock()
        self.state_file = state_file or LED_STATE_FILE
        self.brightness, self.on = 70, False
        try:
            saved = json.loads(self.state_file.read_text())
            self.brightness = min(100, max(0, int(saved.get("brightness", 70))))
            self.on = bool(saved.get("on", False))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        if channel is None:
            chips = list(Path("/sys/devices/platform/fdd70000.pwm/pwm").glob("pwmchip*"))
            channel = chips[0] / "pwm0" if len(chips) == 1 else None
        self.channel = Path(channel) if channel else None
        if self.channel and self.channel.is_dir():
            try:
                if int((self.channel / "period").read_text().strip()) == 0:
                    (self.channel / "period").write_text(str(PWM_PERIOD_NS))
                (self.channel / "enable").write_text("0")
                (self.channel / "period").write_text(str(PWM_PERIOD_NS))
                polarity = self.channel / "polarity"
                if polarity.exists():
                    polarity.write_text("inversed" if os.getenv("EDA_LED_ACTIVE_LOW") == "1" else "normal")
                self._write_output()
                LOG.info("LED PWM ready: %s", self.channel)
            except OSError as exc:
                LOG.warning("LED PWM setup failed: %s", exc)
                self.channel = None
        else:
            LOG.warning("LED PWM0 unavailable")
            self.channel = None

    def _write_output(self):
        duty = round(PWM_PERIOD_NS * self.brightness / 100) if self.on else 0
        (self.channel / "duty_cycle").write_text(str(duty))
        (self.channel / "enable").write_text("1")

    def status(self):
        with self.lock:
            return self.on, self.brightness

    def apply(self, command, value=None):
        with self.lock:
            if self.channel is None:
                return None
            previous = self.on, self.brightness
            if command == "on":
                self.on = True
                if self.brightness == 0:
                    self.brightness = 70
            elif command == "off":
                self.on = False
            elif command == "level":
                self.brightness = max(0, min(100, int(value)))
                self.on = self.brightness > 0
            elif command == "delta":
                self.brightness = max(0, min(100, self.brightness + int(value)))
                self.on = self.brightness > 0
            elif command == "query":
                return self.on, self.brightness
            try:
                self._write_output()
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.state_file.with_suffix(".tmp")
                temporary.write_text(json.dumps({"on": self.on, "brightness": self.brightness}))
                temporary.replace(self.state_file)
                return self.on, self.brightness
            except OSError as exc:
                self.on, self.brightness = previous
                LOG.exception("LED PWM write failed: %s", exc)
                return None


def local_led_answer(question, led):
    intent = led_intent(question)
    if intent is None:
        return None
    if led is None:
        return "灯控暂不可用。"
    state = led.apply(*intent)
    if state is None:
        return "灯控暂不可用。"
    on, brightness = state
    return f"灯已打开，亮度百分之{brightness}。" if on else "灯已关闭。"


class AmbientGate:
    """Track quiet microphone chunks and derive a speech threshold from them."""

    def __init__(self):
        self.floor = max(0.001, float(os.getenv("EDA_VAD_RMS", "0.012")))
        self.multiplier = max(1.2, float(os.getenv("EDA_VAD_MULTIPLIER", "2.5")))
        self.ambient = deque(maxlen=50)

    @property
    def average(self):
        if not self.ambient:
            return 0.0
        values = sorted(self.ambient)
        # Ignore occasional bumps when estimating the steady background level.
        quiet = values[:max(1, int(len(values) * 0.6))]
        return sum(quiet) / len(quiet)

    @property
    def threshold(self):
        return max(self.floor, self.average * self.multiplier)

    def observe(self, rms):
        if len(self.ambient) < 5:
            self.ambient.append(rms)
            return False
        if rms >= self.threshold:
            return True
        self.ambient.append(rms)
        return False

    @property
    def end_threshold(self):
        return max(self.floor * 0.65, self.average * 1.5)


def mood_for_text(text):
    if re.search(r"抱歉|对不起|遗憾|难过|无法|失败", text):
        return "sad"
    if re.search(r"谢谢|太好了|很高兴|你好|欢迎|！|!", text):
        return "happy"
    if "？" in text or "?" in text:
        return "curious"
    return "speaking"


def idle_prompt(display):
    return display.idle_text() if hasattr(display, "idle_text") else "等待唤醒：小嘉"


def render_oled_frame(text, mood, frame, font, mode="face_text"):
    """Render a 128x64 monochrome face and two lines of Chinese text."""
    from PIL import Image, ImageDraw

    image = Image.new("1", (128, 64))
    draw = ImageDraw.Draw(image)
    if mode == "text":
        lines, line = [], ""
        for char in " ".join(text.split()):
            if line and draw.textlength(line + char, font=font) > 126:
                lines.append(line)
                line = char
            else:
                line += char
        if line:
            lines.append(line)
        for row, value in enumerate(lines[:4]):
            draw.text((0, row * 16), value, font=font, fill=255)
        return image
    blink = mood in ("idle", "happy") and frame % 20 in (0, 1)
    if mood == "sleepy":
        draw.line((38, 15, 55, 15), fill=255, width=2)
        draw.line((73, 15, 90, 15), fill=255, width=2)
        draw.text((101, 2), "z", fill=255)
    elif mood == "happy":
        draw.arc((38, 7, 56, 23), 180, 360, fill=255, width=2)
        draw.arc((72, 7, 90, 23), 180, 360, fill=255, width=2)
    elif mood == "sad":
        draw.line((38, 10, 55, 14), fill=255, width=2)
        draw.line((73, 14, 90, 10), fill=255, width=2)
        draw.ellipse((42, 15, 49, 21), fill=255)
        draw.ellipse((79, 15, 86, 21), fill=255)
    elif blink:
        draw.line((38, 15, 55, 15), fill=255, width=2)
        draw.line((73, 15, 90, 15), fill=255, width=2)
    else:
        for left in (38, 73):
            draw.ellipse((left, 8, left + 17, 23), outline=255, width=2)
            shift = 3 if mood == "thinking" and frame % 8 < 4 else 0
            draw.ellipse((left + 6 + shift, 13, left + 10 + shift, 18), fill=255)
    if mood == "listening":
        for offset in (0, 5):
            draw.arc((18 - offset, 8 - offset, 33 + offset, 26 + offset), 75, 285, fill=255)
            draw.arc((95 - offset, 8 - offset, 110 + offset, 26 + offset), 255, 105, fill=255)
        draw.ellipse((60, 26, 68, 31), outline=255)
    elif mood == "thinking":
        draw.line((55, 29, 73, 29), fill=255, width=2)
        for index in range(3):
            draw.ellipse((100 + index * 7, 5 + (index + frame) % 3, 103 + index * 7, 8 + (index + frame) % 3), fill=255)
    elif mood == "speaking":
        if frame % 4 < 2:
            draw.ellipse((55, 25, 73, 32), outline=255, width=2)
        else:
            draw.arc((53, 21, 75, 33), 0, 180, fill=255, width=2)
    elif mood == "curious":
        draw.line((73, 5, 90, 2), fill=255, width=2)
        draw.ellipse((59, 25, 69, 33), outline=255, width=2)
    elif mood == "sad":
        draw.arc((54, 27, 74, 37), 180, 360, fill=255, width=2)
    else:
        breath = 1 if mood == "idle" and frame % 8 >= 4 else 0
        draw.arc((52, 21 + breath, 76, 34 + breath), 0, 180, fill=255, width=2)
    if mode == "face":
        return image.crop((0, 0, 128, 35)).resize((128, 64), Image.Resampling.NEAREST)
    draw.line((0, 35, 127, 35), fill=255)
    lines, line = [], ""
    for char in " ".join(text.split()):
        if line and draw.textlength(line + char, font=font) > 126:
            lines.append(line)
            line = char
        else:
            line += char
    if line:
        lines.append(line)
    for row, value in enumerate(lines[:2]):
        draw.text((0, 36 + row * 14), value, font=font, fill=255)
    return image


def render_menu_frame(page, selected, display_mode, conversation_mode, weather, font,
                      led_status=(False, 70), prompt_enabled=True, llm_model=MODEL):
    from PIL import Image, ImageDraw

    image = Image.new("1", (128, 64))
    draw = ImageDraw.Draw(image)
    if page == "weather":
        draw.text((0, 0), "本地温湿度", font=font, fill=255)
        draw.line((0, 17, 127, 17), fill=255)
        if weather and "temperature_c" in weather:
            draw.text((0, 19), f"温度 {weather['temperature_c']:.1f} C", font=font, fill=255)
            draw.text((0, 39), f"湿度 {weather['humidity_percent']:.1f} %", font=font, fill=255)
        else:
            draw.text((0, 24), "读取中..." if weather is None else "传感器不可用", font=font, fill=255)
    else:
        menu_font = font.font_variant(size=12) if hasattr(font, "font_variant") else font
        display_label = {"face": "纯表情", "face_text": "表情+文字", "text": "纯文字"}[display_mode]
        mode_label = {"wake": "唤醒", "continuous": "连续", "button": "按键"}[conversation_mode]
        led_on, led_level = led_status
        labels = (f"显示 {display_label}", f"对话 {mode_label}",
                  f"灯 {str(led_level) + '%' if led_on else '关'}",
                  f"提示词 {'开' if prompt_enabled else '关'}",
                  f"模型 {'0.6B' if llm_model == MODELS[0] else '1.7B'}", "返回")
        first = max(0, min(selected - 3, len(labels) - 4))
        for row, label in enumerate(labels[first:first + 4]):
            top = row * 16
            if first + row == selected:
                draw.rectangle((0, top, 127, top + 15), outline=255)
            bounds = draw.textbbox((0, 0), label, font=menu_font)
            text_y = top + (16 - (bounds[3] - bounds[1])) // 2 - bounds[1]
            draw.text((3, text_y), label, font=menu_font, fill=255)
    return image


class Display:
    """Animate an optional OLED independently of capture and playback."""

    def __init__(self, led=None):
        self.device = None
        self.next_retry = 0.0
        self.text = "正在启动"
        self.mood = "thinking"
        self.frame = 0
        self.changed = time.monotonic()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.font = None
        self.led = led
        self.page = "face"
        self.menu_index = 0
        self.display_mode = "face_text"
        self.conversation_mode = "wake"
        self.prompt_enabled = True
        self.llm_model = MODEL if MODEL in MODELS else MODELS[1]
        self.answer_cancel = None
        self.interrupt_playback = None
        self.button_armed = False
        self.weather = None
        self.weather_at = 0.0
        try:
            settings = json.loads(SETTINGS_FILE.read_text())
            if settings.get("display_mode") in DISPLAY_MODES:
                self.display_mode = settings["display_mode"]
            if settings.get("conversation_mode") in CONVERSATION_MODES:
                self.conversation_mode = settings["conversation_mode"]
            if isinstance(settings.get("prompt_enabled"), bool):
                self.prompt_enabled = settings["prompt_enabled"]
            if settings.get("llm_model") in MODELS:
                self.llm_model = settings["llm_model"]
        except (FileNotFoundError, ValueError, OSError):
            pass
        try:
            from PIL import ImageFont
            path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
            self.font = ImageFont.truetype(path, 14) if Path(path).exists() else ImageFont.load_default()
        except Exception as exc:
            LOG.warning("OLED font unavailable: %s", exc)
        self._connect()
        self.worker = threading.Thread(target=self._animate, name="oled-animation", daemon=True)
        self.worker.start()
        self.buttons = ButtonMonitor(self.handle_button)

    def save_settings(self):
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = SETTINGS_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps({"display_mode": self.display_mode,
                                             "conversation_mode": self.conversation_mode,
                                             "prompt_enabled": self.prompt_enabled,
                                             "llm_model": self.llm_model}))
            temporary.replace(SETTINGS_FILE)
        except OSError as exc:
            LOG.warning("Could not save settings: %s", exc)

    def handle_button(self, offset):
        """Left/right navigate, center select or arm a single button conversation."""
        changed = False
        interrupted = False
        with self.lock:
            if offset == 2 and self.answer_cancel is not None:
                self.answer_cancel.set()
                self.text, self.mood = "正在停止回答", "idle"
                interrupted = True
            elif offset in (1, 3):
                if self.page == "settings":
                    self.menu_index = (self.menu_index + (1 if offset == 3 else -1)) % len(SETTINGS_ITEMS)
                else:
                    pages = ("face", "weather", "settings")
                    self.page = pages[(pages.index(self.page) + (1 if offset == 3 else -1)) % 3]
                    if self.page == "weather":
                        self.weather_at = 0.0
            elif offset == 2:
                if self.page == "settings":
                    if self.menu_index == 0:
                        self.display_mode = DISPLAY_MODES[(DISPLAY_MODES.index(self.display_mode) + 1) % 3]
                        changed = True
                    elif self.menu_index == 1:
                        self.conversation_mode = CONVERSATION_MODES[(CONVERSATION_MODES.index(self.conversation_mode) + 1) % 3]
                        self.button_armed = False
                        changed = True
                    elif self.menu_index == 2:
                        if self.led:
                            on, level = self.led.status()
                            next_level = ({0: 25, 25: 50, 50: 75, 75: 100,
                                           100: 0}.get(level, 25) if on else 25)
                            self.led.apply("level", next_level)
                    elif self.menu_index == 3:
                        self.prompt_enabled = not self.prompt_enabled
                        changed = True
                    elif self.menu_index == 4:
                        self.llm_model = MODELS[1 - MODELS.index(self.llm_model)]
                        changed = True
                    else:
                        self.page = "face"
                elif self.conversation_mode == "button":
                    self.button_armed = True
                    self.page = "face"
                    self.text, self.mood = "按键聆听中", "listening"
                elif self.page == "weather":
                    self.weather_at = 0.0
                else:
                    self.page = "settings"
            self.changed = time.monotonic()
        if changed:
            self.save_settings()
        if interrupted and self.interrupt_playback:
            self.interrupt_playback()
        LOG.info("button=%s page=%s display=%s conversation=%s prompt=%s model=%s interrupted=%s",
                 offset, self.page, self.display_mode, self.conversation_mode,
                 self.prompt_enabled, self.llm_model, interrupted)

    def mode(self):
        with self.lock:
            return self.conversation_mode

    def system_prompt_enabled(self):
        with self.lock:
            return self.prompt_enabled

    def selected_model(self):
        with self.lock:
            return self.llm_model

    def begin_answer(self):
        with self.lock:
            self.answer_cancel = threading.Event()
            return self.answer_cancel

    def finish_answer(self, cancel):
        with self.lock:
            if self.answer_cancel is cancel:
                self.answer_cancel = None

    def take_button_arm(self):
        with self.lock:
            armed = self.button_armed
            self.button_armed = False
            return armed

    def idle_text(self):
        mode = self.mode()
        if mode == "button":
            return "按中键开始对话"
        if mode == "continuous":
            return "连续聆听中"
        return "等待唤醒：小嘉"

    def _connect(self):
        self.next_retry = time.monotonic() + 10
        try:
            from luma.core.interface.serial import i2c
            from luma.oled.device import ssd1306
            bus = int(os.getenv("EDA_OLED_BUS", "2"))
            address = int(os.getenv("EDA_OLED_ADDRESS", "0x3c"), 0)
            self.device = ssd1306(i2c(port=bus, address=address), width=128, height=64)
            LOG.info("OLED ready: i2c-%s/%#x", bus, address)
        except Exception as exc:
            LOG.warning("OLED unavailable; continuing headless: %s", exc)

    def show(self, text, mood=None):
        with self.lock:
            if text != self.text or (mood and mood != self.mood):
                self.changed = time.monotonic()
            self.text = text
            if mood:
                self.mood = mood

    def _animate(self):
        last_image = None
        while not self.stop.wait(0.25):
            if self.device is None and time.monotonic() >= self.next_retry:
                self._connect()
            if self.device is None or self.font is None:
                continue
            with self.lock:
                text, mood, changed = self.text, self.mood, self.changed
                page, selected = self.page, self.menu_index
                display_mode, conversation_mode = self.display_mode, self.conversation_mode
                prompt_enabled, llm_model = self.prompt_enabled, self.llm_model
                weather, weather_at = self.weather, self.weather_at
                led_status = self.led.status() if self.led else (False, 70)
            if page == "weather" and time.monotonic() - weather_at >= 30:
                try:
                    weather = read_aht10()
                except Exception as exc:
                    LOG.warning("Weather page AHT10 read failed: %s", exc)
                    weather = {}
                with self.lock:
                    self.weather, self.weather_at = weather, time.monotonic()
            if mood == "idle" and time.monotonic() - changed > 60:
                mood = "sleepy"
            try:
                image = (render_oled_frame(text, mood, self.frame, self.font, display_mode)
                         if page == "face" else
                         render_menu_frame(page, selected, display_mode, conversation_mode,
                                           weather, self.font, led_status, prompt_enabled,
                                           llm_model))
                pixels = image.tobytes()
                if pixels != last_image:
                    self.device.display(image)
                    last_image = pixels
                self.frame += 1
            except Exception as exc:
                LOG.warning("OLED update failed: %s", exc)
                self.device = None
                last_image = None

    def close(self):
        self.buttons.close()
        self.stop.set()
        self.worker.join(timeout=2)


class ButtonMonitor:
    """Use kernel GPIO edge events; no userspace polling during inference."""

    def __init__(self, callback):
        self.callback = callback
        self.stop = threading.Event()
        self.process = None
        self.worker = threading.Thread(target=self._run, name="gpio-buttons", daemon=True)
        self.worker.start()

    def _run(self):
        recent = {1: 0.0, 2: 0.0, 3: 0.0}
        while not self.stop.is_set():
            try:
                self.process = subprocess.Popen(
                    ["gpiomon", "-c", "gpiochip3", "-b", "pull-up", "-e", "falling",
                     "-p", "40ms", "-F", "%o", "1", "2", "3"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                )
                LOG.info("GPIO3 A1/A2/A3 button monitor started")
                while not self.stop.is_set() and self.process.poll() is None:
                    ready, _, _ = select.select([self.process.stdout], [], [], 0.5)
                    if not ready:
                        continue
                    line = self.process.stdout.readline().strip()
                    if line in ("1", "2", "3"):
                        offset = int(line)
                        now = time.monotonic()
                        if now - recent[offset] >= 0.15:
                            recent[offset] = now
                            self.callback(offset)
                if not self.stop.is_set():
                    LOG.warning("Button monitor exited: %s", self.process.stderr.read().strip())
            except Exception as exc:
                LOG.warning("Button monitor unavailable: %s", exc)
            finally:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
                if self.process:
                    self.process.stdout.close()
                    self.process.stderr.close()
            self.stop.wait(5)

    def close(self):
        self.stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.worker.join(timeout=2)


def model_files(backend):
    if backend == "rknn":
        return {name: str(NPU / f"{name}.rknn") for name in ("encoder", "decoder", "joiner")} | {
            "tokens": str(NPU / "tokens.txt")
        }
    return {
        "encoder": str(CPU / "encoder-epoch-99-avg-1.int8.onnx"),
        "decoder": str(CPU / "decoder-epoch-99-avg-1.onnx"),
        "joiner": str(CPU / "joiner-epoch-99-avg-1.int8.onnx"),
        "tokens": str(CPU / "tokens.txt"),
    }


class ASR:
    def __init__(self):
        self.gate = AmbientGate()
        preference = (BASE / "asr_backend").read_text().strip() if (BASE / "asr_backend").exists() else "sensevoice"
        self.recognizer = None
        if preference not in ("sensevoice", "rknn", "cpu"):
            raise ValueError(f"Unknown ASR backend: {preference}")
        for backend in ((preference, "cpu") if preference != "cpu" else ("cpu",)):
            try:
                self.load(backend)
                break
            except Exception:
                LOG.exception("ASR %s initialization failed", backend)
        if self.recognizer is None:
            raise RuntimeError("No working ASR backend")

    def load(self, backend):
        if backend == "sensevoice":
            model, tokens = SENSE / "model.rknn", SENSE / "tokens.txt"
            for path in (model, tokens):
                if not path.is_file():
                    raise FileNotFoundError(path)
            self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model), tokens=str(tokens), provider="rknn", num_threads=1,
                sample_rate=16000, language="auto", use_itn=True,
            )
            self.backend = backend
            LOG.info("ASR backend=%s", backend)
            return
        paths = model_files(backend)
        for path in paths.values():
            if not Path(path).is_file():
                raise FileNotFoundError(path)
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            **paths, provider=backend, num_threads=1 if backend == "rknn" else 2,
            sample_rate=16000, enable_endpoint_detection=True,
            rule2_min_trailing_silence=0.8, rule3_min_utterance_length=15,
            decoding_method="greedy_search",
        )
        self.recognizer, self.backend = recognizer, backend
        LOG.info("ASR backend=%s", backend)

    def listen(self, display):
        if self.backend == "sensevoice":
            return self.listen_sensevoice(display)
        stream = self.recognizer.create_stream()
        capture = subprocess.Popen(
            ["arecord", "-q", "-D", MIC, "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        started = last_change = time.monotonic()
        current = ""
        button_mode = getattr(display, "mode", lambda: "wake")() == "button"
        display.show("正在聆听" if button_mode else idle_prompt(display),
                     "listening" if button_mode else "idle")
        try:
            while not STOP.is_set():
                elapsed = time.monotonic() - started
                if (not current and elapsed >= 20) or elapsed >= 30:
                    return current or None
                if capture.poll() is not None:
                    raise RuntimeError(f"arecord exit {capture.returncode}")
                readable, _, _ = select.select([capture.stdout], [], [], 0.5)
                if not readable:
                    continue
                raw = os.read(capture.stdout.fileno(), 3200)
                if not raw:
                    raise RuntimeError("arecord closed")
                samples = np.frombuffer(raw[:len(raw) & ~1], dtype="<i2").astype(np.float32) / 32768
                if not len(samples):
                    continue
                try:
                    stream.accept_waveform(16000, samples)
                    while self.recognizer.is_ready(stream):
                        self.recognizer.decode_stream(stream)
                    text = self.recognizer.get_result_all(stream).text.strip()
                    endpoint = self.recognizer.is_endpoint(stream)
                except Exception:
                    if self.backend != "rknn":
                        raise
                    LOG.exception("NPU inference failed; switching to CPU")
                    self.load("cpu")
                    return None
                if text != current:
                    current = text
                    last_change = time.monotonic()
                    if text:
                        display.show(text, "listening")
                if current and (endpoint or elapsed >= 15 or time.monotonic() - last_change >= 3):
                    return current
            return None
        finally:
            capture.terminate()
            try:
                capture.wait(timeout=2)
            except subprocess.TimeoutExpired:
                capture.kill()
                capture.wait(timeout=2)
            capture.stdout.close()

    def listen_sensevoice(self, display):
        """Energy gate and decode at most 4.5 seconds per NPU request."""
        capture = subprocess.Popen(
            ["arecord", "-q", "-D", MIC, "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        started = time.monotonic()
        pre_roll = deque(maxlen=8)
        chunks, phrases = [], []
        speech, silence, duration, voiced = False, 0.0, 0.0, 0.0
        button_mode = getattr(display, "mode", lambda: "wake")() == "button"
        display.show("正在聆听" if button_mode else idle_prompt(display),
                     "listening" if button_mode else "idle")
        try:
            while not STOP.is_set():
                if time.monotonic() - started >= 20 and not speech:
                    return None
                if time.monotonic() - started >= 30:
                    return " ".join(phrases) or None
                if capture.poll() is not None:
                    raise RuntimeError(f"arecord exit {capture.returncode}")
                readable, _, _ = select.select([capture.stdout], [], [], 0.5)
                if not readable:
                    continue
                raw = os.read(capture.stdout.fileno(), 3200)
                if not raw:
                    raise RuntimeError("arecord closed")
                samples = np.frombuffer(raw[:len(raw) & ~1], dtype="<i2").astype(np.float32) / 32768
                if not len(samples):
                    continue
                step = len(samples) / 16000
                rms = float(np.sqrt(np.mean(samples * samples)))
                if not speech:
                    pre_roll.append(samples)
                    if self.gate.observe(rms):
                        LOG.info("voice start: rms=%.4f ambient=%.4f threshold=%.4f",
                                 rms, self.gate.average, self.gate.threshold)
                        speech = True
                        chunks = list(pre_roll)
                        duration = sum(len(item) for item in chunks) / 16000
                        voiced = step
                        pre_roll.clear()
                        display.show("正在聆听", "listening")
                    elif time.monotonic() - started >= 20:
                        return None
                    continue
                chunks.append(samples)
                duration += step
                if rms >= self.gate.end_threshold:
                    silence = 0.0
                    voiced += step
                else:
                    silence += step
                if duration >= 4.5 or silence >= 0.8:
                    if voiced >= 0.25:
                        display.show("正在识别", "thinking")
                        stream = self.recognizer.create_stream()
                        stream.accept_waveform(16000, np.concatenate(chunks))
                        try:
                            self.recognizer.decode_stream(stream)
                        except Exception:
                            LOG.exception("NPU SenseVoice failed; switching to CPU")
                            self.load("cpu")
                            return None
                        text = stream.result.text.strip()
                        if re.search(r"[A-Za-z0-9\u4e00-\u9fff]", text):
                            phrases.append(text)
                            display.show(text, "listening")
                    if silence >= 0.8 or time.monotonic() - started >= 15:
                        return " ".join(phrases) or None
                    chunks, duration, voiced = [], 0.0, 0.0
            return None
        finally:
            capture.terminate()
            try:
                capture.wait(timeout=2)
            except subprocess.TimeoutExpired:
                capture.kill()
                capture.wait(timeout=2)
            capture.stdout.close()


class TTS:
    def __init__(self):
        self.playback_lock = threading.Lock()
        self.playback_process = None
        choice = (BASE / "tts_backend").read_text().strip() if (BASE / "tts_backend").exists() else "xiao_ya"
        if choice not in ("xiao_ya", "aishell3"):
            raise ValueError(f"Unknown TTS backend: {choice}")
        self.engine = None
        for backend in ((choice, "aishell3") if choice != "aishell3" else ("aishell3",)):
            try:
                directory = VOICE_XIAO if backend == "xiao_ya" else VOICE
                model = "zh_CN-xiao_ya-medium.onnx" if backend == "xiao_ya" else "model.onnx"
                config = sherpa_onnx.OfflineTtsConfig(
                    model=sherpa_onnx.OfflineTtsModelConfig(
                        vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                            model=str(directory / model), lexicon=str(directory / "lexicon.txt"),
                            tokens=str(directory / "tokens.txt"),
                        ), provider="cpu", num_threads=3 if backend == "xiao_ya" else 2,
                    ),
                    rule_fsts=",".join(str(directory / name) for name in ("phone.fst", "date.fst", "number.fst")),
                )
                if not config.validate():
                    raise RuntimeError(f"{backend} model files invalid")
                self.engine = sherpa_onnx.OfflineTts(config)
                self.backend = backend
                LOG.info("TTS backend=%s", backend)
                break
            except Exception:
                LOG.exception("TTS %s initialization failed", backend)
        if self.engine is None:
            raise RuntimeError("No working TTS backend")

    def synthesize(self, text):
        if not text.strip():
            return b""
        spoken = re.sub(r"(?m)^\s*(\d+)[.)、]\s*", r"第\1步，", text)
        spoken = re.sub(r"(?m)^\s*[-•]\s*", "", spoken)
        spoken = re.sub(r"(?m)^\s*#{1,6}\s*", "", spoken)
        spoken = re.sub(r"[*`_~]", "", spoken)
        for acronym, pronunciation in (
            ("EDA", "电子设计自动化"), ("NPU", "神经网络处理器"),
            ("CPU", "处理器"), ("AI", "人工智能"),
        ):
            spoken = re.sub(
                rf"(?<![A-Za-z]){acronym}(?![A-Za-z])", pronunciation,
                spoken, flags=re.IGNORECASE,
            )
        audio = self.engine.generate(spoken, sherpa_onnx.GenerationConfig())
        if not len(audio.samples):
            raise RuntimeError("TTS produced empty audio")
        samples = np.asarray(audio.samples, dtype=np.float32)
        gain = min(4.0, 0.8 / max(float(np.max(np.abs(samples))), 0.01))
        samples = np.clip(samples * gain, -0.95, 0.95)
        playback_rate = 48000
        if audio.sample_rate != playback_rate:
            samples = np.interp(
                np.arange(len(samples) * playback_rate // audio.sample_rate)
                * audio.sample_rate / playback_rate,
                np.arange(len(samples)), samples,
            ).astype(np.float32)
        return (samples * 32767).astype("<i2").tobytes()

    def stop_playback(self):
        with self.playback_lock:
            process = self.playback_process
        if process is not None and process.poll() is None:
            process.terminate()

    def play(self, pcm, cancel=None):
        if not pcm or (cancel is not None and cancel.is_set()):
            return
        process = subprocess.Popen(
            ["aplay", "-q", "-D", SPEAKER, "-f", "S16_LE", "-r", "48000", "-c", "1", "-t", "raw"],
            stdin=subprocess.PIPE,
        )
        with self.playback_lock:
            self.playback_process = process
        try:
            if cancel is not None and cancel.is_set():
                process.terminate()
            process.communicate(input=pcm, timeout=60)
            if process.returncode and not (cancel is not None and cancel.is_set()):
                raise subprocess.CalledProcessError(process.returncode, process.args)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        finally:
            with self.playback_lock:
                if self.playback_process is process:
                    self.playback_process = None

    def speak(self, text, cancel=None):
        if cancel is None or not cancel.is_set():
            self.play(self.synthesize(text), cancel=cancel)


def take_sentences(buffer):
    ready = []
    while True:
        mark = re.search(r"[。！？!?；;\n]|(?<!\d)\.(?!\d)", buffer)
        end = mark.end() if mark else 0
        if not end and len(buffer) >= 12:
            for comma in re.finditer(r"[，,、：:]", buffer):
                if comma.end() >= 8:
                    end = comma.end()
                    break
        if not end and len(buffer) >= 64:
            end = 64
        if not end:
            break
        part, buffer = buffer[:end].strip(), buffer[end:]
        if part:
            ready.append(part)
    return ready, buffer


def llm_messages(question, history, prompt_enabled=True):
    messages = [{"role": "system", "content": PROMPT}] if prompt_enabled else []
    return [*messages, *history, {"role": "user", "content": question}]


def respond(question, history, tts, display, cancel=None):
    cancel = cancel or threading.Event()
    if cancel.is_set():
        return ""
    max_tokens = max(80, min(512, int(os.getenv("EDA_MAX_TOKENS", "256"))))
    prompt_enabled = getattr(display, "system_prompt_enabled", lambda: True)()
    model = getattr(display, "selected_model", lambda: MODEL)()
    payload = json.dumps({
        "model": model,
        "messages": llm_messages(question, history, prompt_enabled),
        "stream": True, "think": False,
        "options": {"num_ctx": 2048, "num_predict": max_tokens, "temperature": 0.4},
    }, ensure_ascii=False).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat", payload, {"Content-Type": "application/json"}
    )
    pieces, pending = [], ""
    done_reason = None
    speech = queue.Queue(maxsize=3)
    audio = queue.Queue(maxsize=2)
    speech_error = []
    display.show("正在思考", "thinking")

    def synthesize_sentences():
        while True:
            sentence = speech.get()
            try:
                if sentence is None:
                    break
                if not speech_error and not STOP.is_set() and not cancel.is_set():
                    pcm = tts.synthesize(sentence)
                    while not speech_error and not STOP.is_set() and not cancel.is_set():
                        try:
                            audio.put((sentence, pcm), timeout=0.2)
                            break
                        except queue.Full:
                            continue
            except Exception as exc:
                speech_error.append(exc)
            finally:
                speech.task_done()
        audio.put(None)

    def play_audio():
        while True:
            item = audio.get()
            try:
                if item is None:
                    return
                if not speech_error and not STOP.is_set() and not cancel.is_set():
                    sentence, pcm = item
                    display.show(sentence, mood_for_text(sentence))
                    tts.play(pcm, cancel=cancel)
            except Exception as exc:
                speech_error.append(exc)
            finally:
                audio.task_done()

    synthesis = threading.Thread(target=synthesize_sentences, name="tts-synthesis", daemon=True)
    playback = threading.Thread(target=play_audio, name="tts-playback", daemon=True)
    playback.start()
    synthesis.start()

    def enqueue(sentence):
        while not STOP.is_set() and not cancel.is_set():
            if speech_error:
                raise speech_error[0]
            try:
                speech.put(sentence, timeout=0.2)
                return
            except queue.Full:
                continue

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            for line in response:
                if STOP.is_set() or cancel.is_set():
                    break
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(data["error"])
                if data.get("done"):
                    done_reason = data.get("done_reason")
                chunk = data.get("message", {}).get("content", "")
                if not chunk:
                    continue
                pieces.append(chunk)
                pending += chunk
                ready, pending = take_sentences(pending)
                for sentence in ready:
                    enqueue(sentence)
        if pending.strip() and not STOP.is_set() and not cancel.is_set():
            enqueue(pending.strip())
    except Exception:
        if not cancel.is_set():
            raise
    finally:
        speech.put(None)
        synthesis.join()
        playback.join()
    if cancel.is_set():
        LOG.info("answer interrupted: model=%s", model)
        return ""
    if speech_error:
        raise speech_error[0]
    result = "".join(pieces).strip()
    if done_reason == "length":
        LOG.warning("LLM answer reached %s token limit", max_tokens)
    if result and not STOP.is_set():
        if re.search(r"无法处理包含特定格式|无法讲述|请.{0,10}(?:重新提问|提供更多信息)", result):
            LOG.info("not retaining an unhelpful answer in conversation history")
        else:
            history.extend(({"role": "user", "content": question},
                            {"role": "assistant", "content": result}))
    return result


def wait_for_llm(model=MODEL):
    """Wait for Ollama and load the cached model before the first utterance."""
    deadline = time.monotonic() + 120
    while not STOP.is_set() and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as response:
                models = json.load(response).get("models", [])
            if not any(item.get("name") == model or item.get("model") == model for item in models):
                raise RuntimeError(f"Local model {model} is missing")
            payload = json.dumps({"model": model, "prompt": "", "stream": False, "keep_alive": -1}).encode()
            request = urllib.request.Request(
                "http://127.0.0.1:11434/api/generate", payload,
                {"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                json.load(response)
            LOG.info("LLM ready: %s", model)
            return
        except Exception as exc:
            LOG.warning("Waiting for local LLM: %s", exc)
            STOP.wait(5)
    raise RuntimeError(f"Local LLM did not become ready: {model}")


def process_utterance(heard, history, tts, display, allow_without_wake=False, led=None):
    question = wake_question(heard)
    if question is None:
        if allow_without_wake:
            question = heard.strip()
        else:
            LOG.info("ignored speech without wake name")
            display.show(idle_prompt(display), "idle")
            return False
    LOG.info("heard: %s", heard)
    cancel = getattr(display, "begin_answer", lambda: threading.Event())()
    try:
        if question:
            local_answer = local_led_answer(question, led)
            if local_answer is None:
                local_answer = local_weather_answer(question)
            if local_answer is not None:
                if not cancel.is_set():
                    display.show(local_answer, mood_for_text(local_answer))
                    tts.speak(local_answer, cancel=cancel)
                    LOG.info("answer: %s", local_answer)
            elif not cancel.is_set():
                result = respond(question, history, tts, display, cancel)
                if result:
                    LOG.info("answer: %s", result)
        else:
            display.show("我在，请说", "happy")
            tts.speak("我在，请说。", cancel=cancel)
    finally:
        getattr(display, "finish_answer", lambda _cancel: None)(cancel)
        display.show(idle_prompt(display), "idle")
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    led = LedController()
    display = Display(led=led)
    try:
        asr, tts = ASR(), TTS()
        display.interrupt_playback = tts.stop_playback
        wait_for_llm(display.selected_model())
        history = deque(maxlen=4)
        history_model = display.selected_model()
        display.show(display.idle_text(), "idle")
        LOG.info("assistant ready: ASR=%s LLM=%s wake=小嘉/小佳", asr.backend, history_model)
        while not STOP.is_set():
            try:
                selected_model = display.selected_model()
                if selected_model != history_model:
                    history.clear()
                    history_model = selected_model
                    LOG.info("LLM switched to %s; cleared conversation history", selected_model)
                mode = display.mode()
                if mode == "button" and not display.take_button_arm():
                    STOP.wait(0.1)
                    continue
                heard = asr.listen(display)
                selected_model = display.selected_model()
                if selected_model != history_model:
                    history.clear()
                    history_model = selected_model
                    LOG.info("LLM switched to %s; cleared conversation history", selected_model)
                if not heard:
                    display.show(display.idle_text(), "idle")
                    continue
                if mode == "continuous" and re.fullmatch(r"[A-Za-z][.!?]?|[啊嗯哦，。！？\s]+", heard.strip()):
                    LOG.info("ignored a short/noise transcript: %s", heard)
                    continue
                process_utterance(heard, history, tts, display,
                                  allow_without_wake=(mode in ("button", "continuous")), led=led)
            except Exception:
                LOG.exception("voice loop error; retrying")
                display.show("遇到问题，重试中", "sad")
                STOP.wait(3)
        LOG.info("assistant stopped")
    finally:
        display.close()


if __name__ == "__main__":
    main()
