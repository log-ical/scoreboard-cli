import struct
import unittest
import zlib

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)
from scoreboard.png import crop_transparent, decode_png, downscale


def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def encode(rows, color=6, plte=None, trns=None):
    """Tiny PNG encoder that cycles through all five filter types.
    rows are lists of pixels, each a tuple of the color type's samples."""
    w, h = len(rows[0]), len(rows)
    n = len(rows[0][0])
    raw, prev = b"", bytes(w * n)
    for y, row in enumerate(rows):
        line = bytes(v for px in row for v in px)
        f = y % 5
        out = bytearray()
        for x in range(len(line)):
            a = line[x - n] if x >= n else 0
            b = prev[x]
            c = prev[x - n] if x >= n else 0
            pred = [0, a, b, (a + b) >> 1, paeth(a, b, c)][f]
            out.append((line[x] - pred) & 255)
        raw += bytes([f]) + bytes(out)
        prev = line

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0)
    extra = (chunk(b"PLTE", plte) if plte else b"") + (chunk(b"tRNS", trns) if trns else b"")
    z = zlib.compress(raw)   # in two IDAT chunks
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + extra + chunk(b"IDAT", z[:len(z) // 2])
            + chunk(b"IDAT", z[len(z) // 2:]) + chunk(b"IEND", b""))


def encode_rgba(rows):
    return encode(rows, 6)


class PngTest(unittest.TestCase):
    def test_round_trip_all_filters(self):
        rows = [[((x * 37 + y) % 256, (y * 53) % 256, (x * y) % 256, 255 if (x + y) % 3 else 0)
                 for x in range(7)] for y in range(10)]
        w, h, px = decode_png(encode_rgba(rows))
        self.assertEqual((w, h), (7, 10))
        self.assertEqual([[tuple(p) for p in r] for r in px], rows)

    def test_other_color_types(self):
        w, h = 6, 10   # 10 rows: every filter type twice
        gray = [[((x * 41 + y * 7) % 256,) for x in range(w)] for y in range(h)]
        self.assertEqual(decode_png(encode(gray, 0))[2], [[(v, v, v, 255) for (v,) in r] for r in gray])
        gray_alpha = [[((x * 41 + y) % 256, (x * y * 13) % 256) for x in range(w)] for y in range(h)]
        self.assertEqual(decode_png(encode(gray_alpha, 4))[2], [[(v, v, v, a) for v, a in r] for r in gray_alpha])
        rgb = [[((x * 37 + y) % 256, (y * 53) % 256, (x * y) % 256) for x in range(w)] for y in range(h)]
        self.assertEqual(decode_png(encode(rgb, 2))[2], [[p + (255,) for p in r] for r in rgb])
        palette = [(i * 3 % 256, i * 5 % 256, i * 7 % 256) for i in range(20)]
        trns = bytes([0, 128, 255, 9])   # shorter than the palette: the rest are opaque
        idx = [[((x + y * 3) % 20,) for x in range(w)] for y in range(h)]
        got = decode_png(encode(idx, 3, bytes(v for p in palette for v in p), trns))
        self.assertEqual(got[:2], (w, h))
        self.assertEqual(got[2], [[palette[i] + (trns[i] if i < len(trns) else 255,) for (i,) in r] for r in idx])

    def test_wraparound_in_filters(self):
        # Values near 0 and 255 make the filters' sums wrap around.
        rows = [[((x * 97 + y * 200) % 256, 255 - (x * y) % 256, (x * 255) % 256, 255 if y % 2 else 1)
                 for x in range(9)] for y in range(15)]
        self.assertEqual(decode_png(encode_rgba(rows))[2], rows)

    def test_downscale_enlarges_and_drops_faint_pixels(self):
        red, faint, clear = (255, 0, 0, 255), (0, 0, 255, 100), (0, 0, 0, 0)
        img = (3, 2, [[red, faint, clear], [red, clear, clear]])
        self.assertEqual(downscale(img, 6, 4), [[(255, 0, 0), (255, 0, 0), None, None, None, None]] * 2
                         + [[(255, 0, 0), (255, 0, 0), None, None, None, None]] * 2)
        # alpha-weighted: a faint blue pixel barely tints the opaque red one
        self.assertEqual(downscale((2, 1, [[red, faint]]), 1, 1), [[(255 * 255 // 355, 0, 255 * 100 // 355)]])

    def test_crop_keeps_opaque_columns_from_any_row(self):
        clear, dot = (0, 0, 0, 0), (9, 9, 9, 41)
        rows = [[clear] * 5 for _ in range(4)]
        rows[1][3] = dot
        rows[2][1] = dot
        rows[3][4] = (9, 9, 9, 40)   # not opaque enough to keep
        w, h, px = crop_transparent(decode_png(encode_rgba(rows)))
        self.assertEqual((w, h), (3, 2))
        self.assertEqual(px, [[clear, clear, dot], [dot, clear, clear]])
        all_clear = (2, 2, [[clear, clear], [clear, clear]])
        self.assertIs(crop_transparent(all_clear), all_clear)

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
