# A PNG reader for the frame frost writes: 8-bit RGB or RGBA, no
# interlacing, decoded with zlib and numpy. Blender's render result loads
# EXR files and nothing else, so the pixels are handed over directly.

import struct
import zlib

import numpy as np


def read_png(path):
    """Returns (width, height, rgb) with rgb a uint8 array of shape
    (height, width, 3), rows from the top."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    position = 8
    width = height = 0
    channels = 3
    idat = []
    while position < len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        body = data[position + 8:position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour_type, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or interlace != 0 or colour_type not in (2, 6):
                raise ValueError("frost writes 8-bit RGB or RGBA PNGs; this is not one")
            channels = 3 if colour_type == 2 else 4
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, stride + 1)
    filters = rows[:, 0]
    out = np.zeros((height, stride), dtype=np.uint8)
    previous = np.zeros(stride, dtype=np.uint8)
    for y in range(height):
        line = rows[y, 1:].astype(np.int32)
        kind = filters[y]
        if kind == 0:
            current = line
        elif kind == 1:
            current = line.copy()
            for x in range(channels, stride):
                current[x] = (current[x] + current[x - channels]) & 255
        elif kind == 2:
            current = (line + previous) & 255
        elif kind == 3:
            current = line.copy()
            prev = previous.astype(np.int32)
            for x in range(stride):
                left = current[x - channels] if x >= channels else 0
                current[x] = (current[x] + ((left + prev[x]) >> 1)) & 255
        elif kind == 4:
            current = line.copy()
            prev = previous.astype(np.int32)
            for x in range(stride):
                a = current[x - channels] if x >= channels else 0
                b = prev[x]
                c = prev[x - channels] if x >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                current[x] = (current[x] + predictor) & 255
        else:
            raise ValueError("unknown PNG filter %d" % kind)
        out[y] = current.astype(np.uint8)
        previous = out[y]
    pixels = out.reshape(height, width, channels)
    return width, height, pixels[:, :, :3]


def srgb_to_linear(rgb8):
    """8-bit sRGB to scene-linear floats, so Blender's Standard view
    transform shows the frame exactly as frost wrote it."""
    c = rgb8.astype(np.float32) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
