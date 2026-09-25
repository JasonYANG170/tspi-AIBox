#!/usr/bin/env bash
set -euo pipefail
image=${1:?image required}
loopdev=$(losetup --find --show --partscan --read-only "$image")
mnt=$(mktemp -d)
cleanup() { umount "$mnt/boot" 2>/dev/null || true; umount "$mnt" 2>/dev/null || true; losetup -d "$loopdev" 2>/dev/null || true; rmdir "$mnt" 2>/dev/null || true; }
trap cleanup EXIT
mount -o ro "${loopdev}p2" "$mnt"
mount -o ro "${loopdev}p1" "$mnt/boot"
cat "$mnt/etc/os-release"
cat "$mnt/etc/passwd" | tail -8
ls -la "$mnt/boot" | head
cat "$mnt/boot/armbianEnv.txt" 2>/dev/null || true
ls "$mnt/usr/bin/python3" "$mnt/usr/bin/ollama" 2>/dev/null || true
