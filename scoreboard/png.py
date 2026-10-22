"""Minimal PNG decoder and image helpers (8-bit, non-interlaced)."""

import struct
import zlib
from itertools import accumulate, repeat
from operator import itemgetter

_A = itemgetter(3)
_OPAQUE = bytes(1 if v > 40 else 0 for v in range(256))   # alpha -> kept by crop
MAX_SIDE = 1024   # pixels; logos are 200x200 (500 unscaled), and each pixel costs ~100 bytes decoded


def decode_png(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, palette, trns = 8, [], None, None
    while pos < len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            palette = [tuple(chunk[i:i + 3]) for i in range(0, len(chunk), 3)]
        elif ctype == b"tRNS":
            trns = chunk
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError("unsupported PNG format")
    if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE):   # an empty image would divide by zero later
        raise ValueError("unsupported PNG size %dx%d" % (w, h))
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    stride = w * channels
    # Inflate no more than the image needs: a few KB can inflate to gigabytes.
    raw = zlib.decompressobj().decompress(b"".join(idat), h * (stride + 1))
    if len(raw) < h * (stride + 1):
        raise ValueError("truncated PNG")
    prev = bytes(stride)
    rows = []
    i = 0
    for _ in range(h):
        ftype = raw[i]
        line = raw[i + 1:i + 1 + stride]
        i += 1 + stride
        if ftype == 1:
            line = _unfilter_sub(line, channels)
        elif ftype == 2:
            line = _add_bytes(line, prev)
        elif ftype == 3:
            line = _unfilter_avg(line, prev, channels)
        elif ftype == 4:
            line = _unfilter_paeth(line, prev, channels)
        rows.append(line)
        prev = line

    # Rows of (r, g, b, a) tuples.
    if color == 6:
        return w, h, [list(zip(*[iter(line)] * 4)) for line in rows]
    if color == 2:
        return w, h, [list(zip(line[0::3], line[1::3], line[2::3], repeat(255))) for line in rows]
    if color == 0:
        return w, h, [list(zip(line, line, line, repeat(255))) for line in rows]
    if color == 4:
        return w, h, [list(zip(line[0::2], line[0::2], line[0::2], line[1::2])) for line in rows]
    rgba = [p + (trns[i] if trns and i < len(trns) else 255,)
            for i, p in enumerate(palette) if len(p) == 3]
    return w, h, [list(map(rgba.__getitem__, line)) for line in rows]


# Unfiltering, one scanline at a time; each returns the reconstructed bytes.
# Sub, Average and Paeth predict from the byte `channels` back in the same
# row, so they run per channel over that channel's bytes.

def _add_bytes(line, prev):
    """Bytewise (line + prev) & 255, all at once on big ints (the Up filter)."""
    n = len(line)
    low, high = int.from_bytes(b"\x7f" * n, "big"), int.from_bytes(b"\x80" * n, "big")
    x, y = int.from_bytes(line, "big"), int.from_bytes(prev, "big")
    # Add the low 7 bits of every byte (no carry can leave a byte), then
    # set each top bit to top(x) ^ top(y) ^ carry-in, dropping the carry out.
    return (((x & low) + (y & low)) ^ ((x ^ y) & high)).to_bytes(n, "big")


def _unfilter_sub(line, channels):
    out = bytearray(len(line))
    for c in range(channels):
        out[c::channels] = bytes(map((255).__and__, accumulate(line[c::channels])))
    return bytes(out)


def _unfilter_avg(line, prev, channels):
    out = bytearray(len(line))
    for c in range(channels):
        vals, a = [], 0
        for v, b in zip(line[c::channels], prev[c::channels]):
            a = (v + ((a + b) >> 1)) & 255
            vals.append(a)
        out[c::channels] = bytes(vals)
    return bytes(out)


def _unfilter_paeth(line, prev, channels):
    out = bytearray(len(line))
    for c in range(channels):
        vals, a, up_left = [], 0, 0
        for v, b in zip(line[c::channels], prev[c::channels]):
            # a: left, b: up, up_left: up-left; p = a + b - up_left
            pa, pb, pc = abs(b - up_left), abs(a - up_left), abs(a + b - 2 * up_left)
            a = (v + (a if pa <= pb and pa <= pc else (b if pb <= pc else up_left))) & 255
            vals.append(a)
            up_left = b
        out[c::channels] = bytes(vals)
    return bytes(out)


def downscale(img, tw, th):
    """Box-filter an (w, h, pixels) image down to tw x th, alpha-weighted."""
    w, h, px = img
    cols = [(tx * w // tw, max(tx * w // tw + 1, (tx + 1) * w // tw)) for tx in range(tw)]
    out = []
    for ty in range(th):
        y0, y1 = ty * h // th, max(ty * h // th + 1, (ty + 1) * h // th)
        band = px[y0:y1]
        row = []
        for x0, x1 in cols:
            r = g = b = a = 0
            for src in band:
                for pr, pg, pb, pa in src[x0:x1]:
                    if pa:   # transparent pixels add nothing
                        r += pr * pa
                        g += pg * pa
                        b += pb * pa
                        a += pa
            if a == 0:
                row.append(None)
            else:
                alpha = a / ((y1 - y0) * (x1 - x0))
                row.append((r // a, g // a, b // a) if alpha >= 110 else None)
        out.append(row)
    return out


def crop_transparent(img):
    w, h, px = img
    # Per row, a mask with 1 where the pixel is opaque enough to keep.
    masks = [bytes(map(_A, row)).translate(_OPAQUE) for row in px]
    ys = [y for y in range(h) if 1 in masks[y]]
    if not ys:
        return img
    # Rows outside ys have no opaque pixels, so OR-ing every row's mask
    # gives the columns that have one in a kept row.
    cols = 0
    for m in masks:
        cols |= int.from_bytes(m, "big")
    cols = cols.to_bytes(w, "big")
    x0, x1 = cols.find(1), cols.rfind(1) + 1
    y0, y1 = ys[0], ys[-1] + 1
    return x1 - x0, y1 - y0, [row[x0:x1] for row in px[y0:y1]]


def primary_color(img):
    """Most common saturated-ish color in a logo, used to tint the score."""
    w, h, px = img
    counts = {}
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            r, g, b, a = px[y][x]
            if a < 200:
                continue
            mx, mn = max(r, g, b), min(r, g, b)
            if mx < 50 or (mn > 200) or mx - mn < 40:
                continue  # skip near-black, near-white, and grays
            key = (r >> 5, g >> 5, b >> 5)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return (220, 220, 220)
    k = max(counts, key=counts.get)
    return tuple(min(255, c * 32 + 16) for c in k)
