#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
work=${WORK_DIR:-"$repo/.cache/image-build"}
out=${OUT_DIR:-"$repo/out"}
base_name=Armbian_26.11.0_rockchip_lckfb-tspi_resolute_6.1.157_server_2026.09.01
base_url="https://github.com/ophub/amlogic-s9xxx-armbian/releases/download/Armbian_resolute_arm64_server_2026.09/${base_name}.img.gz"
base_sha=247d8aad854c3bd0d9ba2fd755678967a6f7e59e91b89e1d331fae4f8bba01e6
archive="$work/${base_name}.img.gz"
image="$work/tspi-AIBox_6.1.157.img"
root="$work/root"
loopdev=
mounted=0

cleanup() {
    set +e
    if (( mounted )); then
        for point in dev/pts dev proc sys boot ''; do
            if [[ -z $point ]]; then target="$root"; else target="$root/$point"; fi
            mountpoint -q "$target" && umount -l "$target"
        done
    fi
    [[ -z $loopdev ]] || losetup -d "$loopdev"
}
trap cleanup EXIT

[[ $(id -u) -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
mkdir -p "$work" "$out" "$root"
if [[ ! -f $archive ]]; then
    curl -fL --retry 5 --retry-all-errors -o "$archive.part" "$base_url"
    mv "$archive.part" "$archive"
fi
echo "$base_sha  $archive" | sha256sum -c -
gzip -dc "$archive" > "$image"
truncate -s 12G "$image"
sgdisk -e "$image"
parted -s "$image" resizepart 2 100%
loopdev=$(losetup --find --show --partscan "$image")
partprobe "$loopdev"
udevadm settle
e2fsck -pf "${loopdev}p2"
resize2fs "${loopdev}p2"
mount "${loopdev}p2" "$root"
mounted=1
mount "${loopdev}p1" "$root/boot"
for point in dev dev/pts proc sys; do
    mkdir -p "$root/$point"
    mount --bind "/$point" "$root/$point"
done
resolv_link=$(readlink "$root/etc/resolv.conf" || true)
rm -f "$root/etc/resolv.conf"
cp -L /etc/resolv.conf "$root/etc/resolv.conf"
bash "$repo/scripts/install-rootfs.sh" "$root" "$repo" "$work"
rm -f "$root/etc/resolv.conf"
if [[ -n $resolv_link ]]; then ln -s "$resolv_link" "$root/etc/resolv.conf"; fi

cleanup
mounted=0
loopdev=
name=tspi-AIBox_lckfb-tspi_resolute_6.1.157_$(date -u +%Y%m%d).img.zst
zstd -T0 -7 -f "$image" -o "$out/$name"
(cd "$out" && sha256sum "$name" > "$name.sha256")
echo "Output: $out/$name"
