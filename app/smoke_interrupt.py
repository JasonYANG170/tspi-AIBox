#!/usr/bin/env python3
"""Check that the center button stops streamed speech and discards the answer."""

import json
import threading
from collections import deque

import ai


class Response:
    def __init__(self, release):
        self.release = release

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        yield json.dumps({"message": {"content": "第一句回答。"}}).encode()
        self.release.wait(5)
        yield json.dumps({"message": {"content": "不应继续朗读。"}}).encode()


class Speech:
    def __init__(self):
        self.playing = threading.Event()
        self.stopped = threading.Event()
        self.play_count = 0

    def synthesize(self, text):
        return text.encode()

    def play(self, pcm, cancel=None):
        self.play_count += 1
        self.playing.set()
        cancel.wait(5)

    def stop_playback(self):
        self.stopped.set()


def main():
    release = threading.Event()
    requests = []
    original = ai.urllib.request.urlopen

    def fake_urlopen(request, timeout=None):
        requests.append(json.loads(request.data))
        return Response(release)

    ai.urllib.request.urlopen = fake_urlopen
    display = ai.Display.__new__(ai.Display)
    display.lock = threading.Lock()
    display.page = "face"
    display.menu_index = 0
    display.display_mode = "face_text"
    display.conversation_mode = "continuous"
    display.prompt_enabled = True
    display.llm_model = ai.MODELS[0]
    display.text = ""
    display.mood = "idle"
    display.changed = 0.0
    speech = Speech()
    display.interrupt_playback = speech.stop_playback
    cancel = display.begin_answer()
    history = deque(maxlen=4)
    result = []
    worker = threading.Thread(target=lambda: result.append(
        ai.respond("介绍一下自己", history, speech, display, cancel)))
    try:
        worker.start()
        assert speech.playing.wait(5), "playback did not start"
        display.handle_button(2)
        release.set()
        worker.join(5)
        assert not worker.is_alive(), "answer did not stop"
        assert result == [""] and not history
        assert cancel.is_set() and speech.stopped.is_set()
        assert speech.play_count == 1
        assert requests[0]["model"] == ai.MODELS[0]
        print("center button interrupted streaming answer and playback")
    finally:
        release.set()
        worker.join(5)
        ai.urllib.request.urlopen = original


if __name__ == "__main__":
    main()
