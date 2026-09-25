#!/usr/bin/env python3
"""Export PWM0 on GPIO0_B7 and grant the assistant access to its controls."""

import grp
import os
import pwd
import time
from pathlib import Path


def main():
    directory = Path("/sys/devices/platform/fdd70000.pwm/pwm")
    chips = list(directory.glob("pwmchip*"))
    if len(chips) != 1:
        raise RuntimeError(f"Expected PWM0 controller at {directory}, found {chips}")
    chip = chips[0]
    channel = chip / "pwm0"
    if not channel.exists():
        (chip / "export").write_text("0")
        for _ in range(50):
            if channel.exists():
                break
            time.sleep(0.02)
    if not channel.exists():
        raise RuntimeError("PWM0 channel did not appear")
    uid = pwd.getpwnam("yang").pw_uid
    gid = grp.getgrnam("yang").gr_gid
    for name in ("period", "duty_cycle", "enable", "polarity"):
        path = channel / name
        os.chown(path, uid, gid)
        os.chmod(path, 0o660)
    print(channel, flush=True)


if __name__ == "__main__":
    main()
