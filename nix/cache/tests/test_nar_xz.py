import lzma
import subprocess
import sys
import unittest
from pathlib import Path

NAR_XZ = Path(__file__).resolve().parents[1] / "nar_xz.py"


def compress(data: bytes) -> bytes:
    return subprocess.run(
        [sys.executable, str(NAR_XZ)],
        input=data, capture_output=True, check=True,
    ).stdout


class NarXzTest(unittest.TestCase):
    def test_round_trip(self):
        data = b"hello world\n" * 100000  # ~1.2 MiB 可压缩
        self.assertEqual(lzma.decompress(compress(data)), data)

    def test_xz_magic(self):
        self.assertEqual(compress(b"abc")[:6], b"\xfd7zXZ\x00")

    def test_empty_input(self):
        self.assertEqual(lzma.decompress(compress(b"")), b"")


if __name__ == "__main__":
    unittest.main()
