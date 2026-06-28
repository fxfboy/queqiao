# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

QueQiao (鹊桥) is a tool for transferring **any file** across an **air gap** (内网/外网 — intranet/extranet) using QR codes that are displayed on screen, photographed, and decoded on the other side. The transport is a camera; there is no network path between the two sides.

The project's scope is **only the QR transfer** — it does not care how the input is produced. `encoder.py` takes any file and renders a QR grid as HTML; `decoder.py` photographs it back into a byte-identical file. Generating a repo diff is an *optional* convenience in `make_diff.py`, not part of the core.

## Commands

Everything routes through `run.sh` (Linux/macOS/Git Bash) or `run.bat` (Windows CMD). Dependencies are declared in **`pyproject.toml`** and managed by [uv](https://docs.astral.sh/uv/); the run scripts auto-create `.venv` and `uv sync` on first run, then dispatch via `uv run`. There is no `requirements.txt` (uv pins exact versions in `uv.lock`). The scripts still install the native zbar lib (brew/apt/yum), which uv cannot manage.

```bash
./run.sh encode <file> -o qr.html              # any file → QR HTML
./run.sh decode photo1.jpg photo2.jpg -o out   # zxing-cpp decoder (default, no native lib)
./run.sh decode photos -o out --backend pyzbar # opt-in pyzbar (needs system zbar)
./run.sh diff <base-dir> <target-dir> -o d.patch  # OPTIONAL: dir diff → patch file
./run.sh test                                  # → test_roundtrip.py (byte roundtrip)
./run.sh verify                                # → verify_full.py (real QR via pyzbar)
```

Direct invocation (via `uv run`, which auto-syncs deps; or `python` after `source .venv/bin/activate`):

```bash
uv run python encoder.py <file> -o qr.html [--cols N] [--qr-size N] [--chunk-size N]
uv run python decoder.py <images-or-dirs...> -o out [--backend zxing|pyzbar] [--debug]
uv run python decode_pyzbar.py [image_dir=bid] [out=restored.out] # legacy pyzbar path, scans PNGs in a DIR
uv run python make_diff.py <base> <target> -o d.patch [--ext ...] [--no-gitignore] [--ignore ...]
```

### Tests

Tests are **plain assert-based scripts, not pytest** — there is no per-test selection; run the whole file. `python -m pytest` will not collect them meaningfully.

- `test_roundtrip.py` — byte-roundtrip of the pipeline (arbitrary bytes → `encode_chunks` → HTML → simulated base85 decode → bytes). No diff or image deps. This is `run.sh test`.
- `verify_full.py` / `test_qr_roundtrip.py` — render **real** QR PNGs and read them back with **pyzbar**, asserting byte-identical output. Self-contained: they encode an in-memory byte blob (no external fixtures). `run.sh verify` runs `verify_full.py`. (Run directly with `DYLD_LIBRARY_PATH=/opt/homebrew/lib` set so pyzbar finds libzbar.)
- `test_make_diff.py` — directory-comparison logic for `make_diff.py`. `test_cli.py` — subprocess smoke tests for the three CLIs. (Neither is wired into `run.sh`; run with `uv run python`.)

## Architecture

### The pipeline (and its exact inverse)

```
encoder.py:  read_bytes(input file) → lzma(xz, preset 9|EXTREME) → split into chunks
             → build metadata JSON chunk (index 0) + data chunks (1..N)
             → prepend 12-byte header w/ checksum → base85 → qrcode → HTML grid
decoder.py:  photo(s)/dir(s) → QR backend (zxing-cpp default, pyzbar optional)
             → base85 decode → parse header / verify
             → extract metadata from index 0 → reassemble data chunks (1..N)
             → lzma decompress → verify whole-file SHA256 → write raw bytes
```

base85 is used for the **QR payload** (denser than base64); base64 is used separately only to inline PNGs into the HTML.

The pipeline is **byte-agnostic**: the encoder reads raw bytes and the decoder writes raw bytes (no utf-8 encode/decode), so a text file in yields a byte-identical text file out, and binaries work too.

### chunk-size is transport-dependent (the default is deliberately conservative)

QR count = ceil(compressed_size / chunk-size). The default `--chunk-size 800` balances density and reliability for most transfers. For **screenshot** transfer (pixel-perfect, lossless) you can go much larger — up to ~1800, where a single QR hits version 40 (the max; a chunk-size of ~2000+ raises a v41 error). Pair large chunks with a bigger `--qr-size` (>= the QR's native pixel size) or the screenshot's downsampling blurs the dense modules and decoding fails. Measured on a ~487KB text file: 235 codes (chunk-size 400) vs 53 codes (`chunk-size 1800`). See `output/RESULTS.md`.

### The chunk binary format is the load-bearing contract

Every chunk is `MAGIC(2) | index(2,>H) | total(2,>H) | datalen(2,>H) | checksum(4) | data(N)` — a 12-byte header where `checksum = sha256(data)[:4]`. This struct is **hand-duplicated, not shared**, across six places:

- `encoder.py` → `encode_chunks()` (writer)
- `decoder.py` → `decode_single_chunk()`
- `decode_pyzbar.py` → `decode_single_chunk()`
- `test_roundtrip.py`, `test_qr_roundtrip.py`, `verify_full.py` (inline parsers)

**If you change the header layout, magic, checksum, or struct format, you must update all of these in lockstep.** `make_diff.py` does **not** touch the wire format and is not part of this set.

**Chunk index 0 is a metadata chunk** whose payload is compact JSON:
`{"version":1,"filename":"input.txt","size":12345,"sha256":"abcdef...","compressed_size":5678}`
Data chunks occupy indices 1..N. The `total` field in every header = N+1 (metadata + data). Decoders extract metadata from index 0, merge only `range(1, total)` for data, then verify the whole-file SHA256 after decompression. If index 0 is missing, decoders warn but proceed without verification.

### Reassembly is order-independent by design

The `index`/`total` fields in the header — not image position — drive reconstruction. Decoders dedupe by index, drop chunks failing the SHA256 check, and abort listing any missing indices. Consequence: `decoder.py`'s `sort_qr_by_position()` is essentially cosmetic, and `decode_pyzbar.py` skips sorting entirely. Multi-photo decode works by concatenating all detected QRs across all images into one pool.

### Decoder backends live in `qr_backends/`

Backends are pluggable. Each is one module that subclasses `qr_backends.base.QRDecoderAdapter` and implements `decode_image(path) -> list[QRDecodeResult]`. Registration is explicit in `qr_backends/__init__.py` — the dict order defines `available_backends()` order, and `DEFAULT_BACKEND` is the CLI default. `decoder.py` only talks to the registry (`get_backend(name)`); it does not know which backends exist.

To add a new backend: write `qr_backends/<name>_backend.py`, then import + register it in `qr_backends/__init__.py`. The CLI's `--backend` choices update automatically.

Currently shipped:
- **zxing** (`--backend zxing`, **default**): pure-wheel C++ port of ZXing (`pip install zxing-cpp`) — **no native system library to install**. Reads PIL images directly and exposes raw payload bytes via `barcode.bytes` (no utf-8 round-trip), and gives a 4-corner `position` we map to a bounding box. On pixel-perfect screenshot transfer (queqiao's primary use case) it is ~10–14× faster than pyzbar at 100% accuracy across chunk-sizes 800/1500/1800; on degraded photos (downsample + Gaussian blur + JPEG) the crash threshold is the same as pyzbar — neither offers a robustness edge on dense v40-class codes once downsampling drops below ~30% with blur. See `bench_backends.py` for the reproducible benchmark.
- **pyzbar** (`--backend pyzbar`): opt-in legacy backend that wraps the **native zbar library** (`brew install zbar`; macOS also needs `DYLD_LIBRARY_PATH=/opt/homebrew/lib`, which `run.sh` sets). Kept as a fallback for specific samples where zxing fails to detect — currently no such samples are documented.

Switch to pyzbar only when you have a concrete sample that zxing cannot decode.

### Directory comparison gotchas (`make_diff.py`)

- **Argument order is the diff direction**: `dir1` = base (external), `dir2` = target (internal). Diff is dir1→dir2, applied with `patch -p1`. Swapping the args silently produces a reversed patch.
- **All dotfiles/dirs are skipped unconditionally** in `collect_files()` (anything starting with `.`) — this happens *before* gitignore is consulted, so `.gitignore`, `.env`, etc. are never diffed regardless of flags.
- **gitignore support is partial**: negation patterns (`!`) are explicitly ignored; matching is a hand-rolled `fnmatch` approximation, not full gitignore semantics. `.gitignore` files from *both* dirs are merged.
- Binary/undecodable files (utf-8 read fails) are counted and skipped, never diffed.
