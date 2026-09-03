#!/usr/bin/env python3
"""Streaming xz compressor (FORMAT_XZ, preset 1) — stdin to stdout.

Used by nix/cache/post/push.py to compress NAR dumps without depending on
the `xz` binary (Linux/macOS CI runners both ship python3).
"""
import lzma
import sys

CHUNK = 1 << 20  # 1 MiB


def main() -> None:
    compressor = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=1)
    while True:
        chunk = sys.stdin.buffer.read(CHUNK)
        if not chunk:
            break
        out = compressor.compress(chunk)
        if out:
            sys.stdout.buffer.write(out)
    sys.stdout.buffer.write(compressor.flush())


if __name__ == "__main__":
    main()
