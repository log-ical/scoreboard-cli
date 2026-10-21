"""Minimal PNG decoder and image helpers (8-bit, non-interlaced)."""

import struct
import zlib


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
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    raw = zlib.decompress(b"".join(idat))
    stride = w * channels
    prev = bytearray(stride)
    rows = []
    i = 0
    for _ in range(h):
        ftype = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if ftype == 1:
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 255
        elif ftype == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif ftype == 3:
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif ftype == 4:
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                b = prev[x]
                c = prev[x - channels] if x >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 255
        rows.append(line)
        prev = line

    pixels = []  # list of rows of (r, g, b, a)
    for line in rows:
        out = []
        for x in range(w):
            p = line[x * channels:(x + 1) * channels]
            if color == 6:
                out.append(tuple(p))
            elif color == 2:
                out.append((p[0], p[1], p[2], 255))
            elif color == 0:
                out.append((p[0], p[0], p[0], 255))
            elif color == 4:
                out.append((p[0], p[0], p[0], p[1]))
            else:
                r, g, b = palette[p[0]]
                a = trns[p[0]] if trns and p[0] < len(trns) else 255
                out.append((r, g, b, a))
        pixels.append(out)
    return w, h, pixels


def downscale(img, tw, th):
    """Box-filter an (w, h, pixels) image down to tw x th, alpha-weighted."""
    w, h, px = img
    out = []
    for ty in range(th):
        y0, y1 = ty * h // th, max(ty * h // th + 1, (ty + 1) * h // th)
        row = []
        for tx in range(tw):
            x0, x1 = tx * w // tw, max(tx * w // tw + 1, (tx + 1) * w // tw)
            r = g = b = a = 0
            n = 0
            for y in range(y0, y1):
                src = px[y]
                for x in range(x0, x1):
                    pr, pg, pb, pa = src[x]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
                    n += 1
            if a == 0:
                row.append(None)
            else:
                alpha = a / n
                row.append((r // a, g // a, b // a) if alpha >= 110 else None)
        out.append(row)
    return out


def crop_transparent(img):
    w, h, px = img
    ys = [y for y in range(h) if any(p[3] > 40 for p in px[y])]
    xs = [x for x in range(w) if any(px[y][x][3] > 40 for y in ys)]
    if not ys or not xs:
        return img
    x0, x1, y0, y1 = xs[0], xs[-1] + 1, ys[0], ys[-1] + 1
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
