import struct
import unittest
import zlib

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)
from scoreboard.png import crop_transparent, decode_png, downscale


def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def encode_rgba(rows):
    """Tiny PNG encoder that cycles through all five filter types."""
    w, h = len(rows[0]), len(rows)
    raw, prev = b"", bytes(w * 4)
    for y, row in enumerate(rows):
        line = bytes(v for px in row for v in px)
        f = y % 5
        out = bytearray()
        for x in range(len(line)):
            a = line[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            pred = [0, a, b, (a + b) >> 1, paeth(a, b, c)][f]
            out.append((line[x] - pred) & 255)
        raw += bytes([f]) + bytes(out)
        prev = line

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class PngTest(unittest.TestCase):
    def test_round_trip_all_filters(self):
        rows = [[((x * 37 + y) % 256, (y * 53) % 256, (x * y) % 256, 255 if (x + y) % 3 else 0)
                 for x in range(7)] for y in range(10)]
        w, h, px = decode_png(encode_rgba(rows))
        self.assertEqual((w, h), (7, 10))
        self.assertEqual([[tuple(p) for p in r] for r in px], rows)

    def test_crop_and_downscale(self):
        clear, red = (0, 0, 0, 0), (255, 0, 0, 255)
        rows = [[clear] * 6 for _ in range(6)]
        for y in (2, 3):
            for x in (1, 2, 3, 4):
                rows[y][x] = red
        w, h, px = crop_transparent(decode_png(encode_rgba(rows)))
        self.assertEqual((w, h), (4, 2))
        self.assertEqual(downscale((w, h, px), 2, 1), [[(255, 0, 0), (255, 0, 0)]])

    def test_rejects_non_png(self):
        with self.assertRaises(ValueError):
            decode_png(b"GIF89a....")


if __name__ == "__main__":
    unittest.main()
