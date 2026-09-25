#!/usr/bin/env python3
"""Read AHT10 temperature and humidity on the TaishanPi I2C bus."""

import argparse
import json
import time

from smbus2 import SMBus, i2c_msg


def read_bytes(bus, address, count):
    message = i2c_msg.read(address, count)
    bus.i2c_rdwr(message)
    return bytes(message)


def write_bytes(bus, address, data):
    bus.i2c_rdwr(i2c_msg.write(address, data))


def measure(bus, address):
    status = read_bytes(bus, address, 1)[0]
    if not status & 0x08:
        write_bytes(bus, address, [0xE1, 0x08, 0x00])
        time.sleep(0.02)
        status = read_bytes(bus, address, 1)[0]
        if not status & 0x08:
            raise RuntimeError(f"AHT10 calibration not ready: status=0x{status:02x}")

    write_bytes(bus, address, [0xAC, 0x33, 0x00])
    time.sleep(0.08)
    for _ in range(50):
        data = read_bytes(bus, address, 6)
        if not data[0] & 0x80:
            break
        time.sleep(0.01)
    else:
        raise TimeoutError("AHT10 measurement remained busy")

    humidity_raw = (data[1] << 12) | (data[2] << 4) | (data[3] >> 4)
    temperature_raw = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]
    humidity = humidity_raw * 100 / 0x100000
    temperature = temperature_raw * 200 / 0x100000 - 50
    if not 0 <= humidity <= 100 or not -40 <= temperature <= 85:
        raise ValueError(f"AHT10 reading outside expected range: {data.hex(' ')}")
    return {
        "temperature_c": round(temperature, 2),
        "humidity_percent": round(humidity, 2),
        "status": f"0x{data[0]:02x}",
        "raw": data.hex(" "),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bus", type=int, default=2)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x38)
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()
    with SMBus(args.bus) as bus:
        for index in range(args.count):
            result = measure(bus, args.address)
            result["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S %z")
            result["bus"] = args.bus
            result["address"] = f"0x{args.address:02x}"
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if index + 1 < args.count:
                time.sleep(1)


if __name__ == "__main__":
    main()
