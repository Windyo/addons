#!/usr/bin/env bash
# nv-branch.sh <version or major, e.g. 570>: point /opt/nvidia/{VERSION,kernel-src,lib,bin} at that
# driver branch in the image (/opt/nvidia/branches/<version>). Prints the full version; exits 1 and
# changes nothing if the image doesn't have it.
set -euo pipefail
d=$(ls -d /opt/nvidia/branches/"${1:?version}"* 2>/dev/null | head -n 1)
[ -n "$d" ] || { echo "no driver branch $1 in this image" >&2; exit 1; }
v=${d##*/}
for n in VERSION kernel-src lib bin; do ln -sfn "branches/$v/$n" "/opt/nvidia/$n"; done
ldconfig
echo "$v"
