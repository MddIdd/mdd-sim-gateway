#!/bin/sh
# Print the content fingerprint used to match a distributed Engine image to this checkout.
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
engine_dir="$repo_dir/engine"
kind=${1:-}

case "$kind" in
  runtime)
    runtime_files="pin_keeper.py ami_usim.py swu_ike.py outer_transport.py log_capture.py vowifi_trace.py render.py notify.py entrypoint.sh"
    {
      for file in $runtime_files; do
        [ -f "$engine_dir/$file" ] && cat "$engine_dir/$file"
      done
      find "$engine_dir/templates" -type f 2>/dev/null | LC_ALL=C sort |
        while IFS= read -r template; do cat "$template"; done
    } | sha256sum | cut -d' ' -f1
    ;;
  base)
    {
      cat "$engine_dir/Dockerfile"
      printf 'pcsc=%s\n' "${PCSC_VERSION:-2.3.3}"
      find "$engine_dir/patches" -type f 2>/dev/null | LC_ALL=C sort |
        while IFS= read -r patch; do cat "$patch"; done
    } | sha256sum | cut -d' ' -f1
    ;;
  *)
    printf 'usage: %s runtime|base\n' "$0" >&2
    exit 2
    ;;
esac
