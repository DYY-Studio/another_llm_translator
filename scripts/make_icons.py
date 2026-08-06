#!/usr/bin/env python3
"""Generate the placeholder Tauri icon set with the standard library only."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src-tauri" / "icons"
BACKGROUND = (30, 41, 59, 255)
ACCENT = (99, 179, 237, 255)
CLEAR = (0, 0, 0, 0)


def pixel(size: int, x: int, y: int) -> tuple[int, int, int, int]:
    margin = max(1, size // 16)
    if margin <= x < size - margin and margin <= y < size - margin:
        inner = size - 2 * margin
        if (
            abs(x + 0.5 - size / 2) < inner * 0.18
            or abs(y + 0.5 - size / 2) < inner * 0.18
        ):
            return ACCENT
    if x == 0 or y == 0 or x == size - 1 or y == size - 1:
        return CLEAR
    return BACKGROUND


def png_bytes(size: int) -> bytes:
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            rows.extend(struct.pack("4B", *pixel(size, x, y)))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )


def icns_bytes(pngs: dict[str, bytes]) -> bytes:
    chunks = b"".join(
        struct.pack(">4sI", kind.encode(), len(payload) + 8) + payload
        for kind, payload in pngs.items()
    )
    return b"icns" + struct.pack(">I", len(chunks) + 8) + chunks


def ico_bytes(png: bytes) -> bytes:
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    sizes = {32: "32x32.png", 128: "128x128.png", 256: "128x128@2x.png"}
    pngs: dict[str, bytes] = {}
    for size, filename in sizes.items():
        data = png_bytes(size)
        (ROOT / filename).write_bytes(data)
        pngs[filename] = data
    (ROOT / "icon.icns").write_bytes(
        icns_bytes(
            {
                "ic07": png_bytes(128),
                "ic08": png_bytes(256),
                "ic09": png_bytes(512),
                "ic10": png_bytes(1024),
                "ic11": png_bytes(64),
                "ic13": png_bytes(512),
            }
        )
    )
    (ROOT / "icon.ico").write_bytes(ico_bytes(png_bytes(256)))
    print(f"icons written to {ROOT}")


if __name__ == "__main__":
    main()
