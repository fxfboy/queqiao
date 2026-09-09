# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

QueQiao (鹊桥) is a tool for transferring **any file** across an **air gap** (内网/外网 — intranet/extranet) using QR codes that are displayed on screen, photographed, and decoded on the other side. The transport is a camera; there is no network path between the two sides.

The project's scope is **only the QR transfer** — it does not care how the input is produced. The package lives in `src/queqiao/`: `queqiao/encoder.py` takes any file and renders a QR grid as HTML; `queqiao/decoder.py` photographs it back into a byte-identical file. Generating a repo diff is an *optional* convenience in `queqiao/make_diff.py`, not part of the core. Tests live in `tests/` (plain scripts, not a package).

## Commands

Everything routes through `run.sh` (Linux/macOS/Git Bash) or `run.bat` (Windows CMD), which dispatch to the single console script `queqiao` (subcommands: `encode` / `decode` / `diff` / `stream` / `receive`) defined in `src/queqiao/cli.py`. The project **is an installable package** (`pipx install queqiao[pyzbar]`); in-repo, dependencies are declared in **`pyproject.toml`** and managed by [uv](https://docs.astral.sh/uv/); the run scripts auto-create `.venv` and `uv sync --extra pyzbar` on first run, then dispatch via `uv run`. There is no `requirements.txt` (uv pins exact versions in `uv.lock`). Native libraries are outside uv's reach: the scripts install the zbar lib (brew/apt/yum) for pyzbar, and the JAB Code tools must be built by hand (see the jab backend below).

```bash
./run.sh encode <file> -o qr.html              # any file → QR HTML
./run.sh encode <file> -o jab.html --backend jab  # JAB Code (color) instead of QR
./run.sh decode photo1.jpg photo2.jpg -o out   # zxing-cpp decoder (default, no native lib)
./run.sh decode photos -o out --backend pyzbar # opt-in pyzbar (needs system zbar)
./run.sh decode jab.png -o out --backend jab   # JAB Code (needs native jabcodeReader)
./run.sh diff <base-dir> <target-dir> -o d.patch  # OPTIONAL: dir diff → patch file
./run.sh test                                  # → test_roundtrip.py (byte roundtrip)
./run.sh verify                                # → verify_full.py (real QR via pyzbar)
```

Direct invocation (via `uv run`, which auto-syncs deps; or `python` after `source .venv/bin/activate`):

```bash
uv run queqiao encode <file> -o qr.html [--cols N] [--qr-size N] [--chunk-size N] [--no-open]
                                       [--backend qr|jab] [--jab-colors 4|8]
                                       [--jab-module-size N] [--jab-ecc-level 1..10]
uv run queqiao decode <images-or-dirs...> -o out [--backend zxing|pyzbar|jab] [--debug]
uv run queqiao diff <base> <target> -o d.patch [--ext ...] [--no-gitignore] [--ignore ...]
uv run queqiao stream <file> [--blocklen N] [--ecc L|M|Q|H] [--fps N]   # fountain playback window
uv run queqiao receive [-o FILE] [--screen] [--reselect]                # screen-capture receiver
```

**`--backend` means different things on the two sides, and the value spaces do not overlap.** On `encode` it selects the *symbology* and is a hard-coded `('qr', 'jab')` choice — it does **not** consult the `qr_backends` registry. On `decode` it selects the *decode implementation* and comes from `available_backends()` (`zxing` / `pyzbar` / `jab`). So `--backend qr` is an encode-only value and `--backend zxing` is a decode-only value; only `jab` is valid on both sides.

### Tests

Tests are **plain assert-based scripts, not pytest** — there is no per-test selection; run the whole file. `python -m pytest` will not collect them meaningfully.

- All test scripts live in `tests/` and there is a runner: `uv run python tests/run_all.py` executes every suite in dependency order and stops at the first failure (it also sets `DYLD_LIBRARY_PATH` for the pyzbar cases on macOS).
- `tests/test_roundtrip.py` — byte-roundtrip of the pipeline (arbitrary bytes → `encode_chunks` → HTML → simulated base85 decode → bytes). No diff or image deps. This is `run.sh test`.
- `tests/verify_full.py` / `tests/test_qr_roundtrip.py` — render **real** QR PNGs and read them back with **pyzbar**, asserting byte-identical output. Self-contained: they encode an in-memory byte blob (no external fixtures). `run.sh verify` runs `tests/verify_full.py`. (When run directly, set `DYLD_LIBRARY_PATH=/opt/homebrew/lib` so pyzbar finds libzbar; `tests/run_all.py` does this for you.)
- `tests/test_make_diff.py` — directory-comparison logic for `make_diff.py`. `tests/test_cli.py` — subprocess smoke tests for the CLIs, spawned as `python -m queqiao.encoder` / `python -m queqiao.decoder` (run with `uv run python`). Its pyzbar case needs libzbar reachable: on macOS run it as `DYLD_LIBRARY_PATH=/opt/homebrew/lib uv run python tests/test_cli.py` — otherwise that case exits 1 with an *empty* stderr, because the "pyzbar backend is unavailable" hint goes to stdout.
- `tests/test_jab_backend.py` — the JAB Code CLI bridge, **without needing the native tools**: it writes throwaway Python scripts that impersonate `jabcodeWriter`/`jabcodeReader` and points `QUEQIAO_JAB_WRITER`/`QUEQIAO_JAB_READER` at them. Also covers the `payload_encoding='raw'` chunk roundtrip. Not wired into `run.sh`; run with `uv run python tests/test_jab_backend.py`.

## Architecture

### The pipeline (and its exact inverse)

```
encoder.py:  read_bytes(input file) → lzma(xz, preset 9|EXTREME) → split into chunks
             → build metadata JSON chunk (index 0) + data chunks (1..N)
             → prepend 12-byte header w/ checksum → base85 (QR) | raw bytes (JAB)
             → qrcode | jabcodeWriter → HTML grid
decoder.py:  photo(s)/dir(s) → decode backend (zxing-cpp default, pyzbar/jab optional)
             → base85 decode (or raw passthrough) → parse header / verify
             → extract metadata from index 0 → reassemble data chunks (1..N)
             → lzma decompress → verify whole-file SHA256 → write raw bytes
```

**The transport encoding of the chunk is a per-backend property, not a pipeline constant.** For QR the chunk bytes go through base85 (denser than base64); for JAB Code the chunk bytes are written **raw**, skipping base85's ~25% inflation entirely. The decode side keys off `adapter.payload_encoding` (`'base85'` default on `QRDecoderAdapter`, `'raw'` on `JabCodeDecoder`), which `decoder.py` passes into `decode_and_merge_chunks()`. base64 is used separately, on both paths, only to inline the PNGs into the HTML.

The 12-byte header itself is **identical on both paths** — only the layer that carries it differs.

The pipeline is **byte-agnostic**: the encoder reads raw bytes and the decoder writes raw bytes (no utf-8 encode/decode), so a text file in yields a byte-identical text file out, and binaries work too.

### chunk-size is transport-dependent (the default is deliberately conservative)

QR count = ceil(compressed_size / chunk-size). The default `--chunk-size 800` balances density and reliability for most transfers. For **screenshot** transfer (pixel-perfect, lossless) you can go much larger — up to ~1800, where a single QR hits version 40 (the max; a chunk-size of ~2000+ raises a v41 error). Pair large chunks with a bigger `--qr-size` (>= the QR's native pixel size) or the screenshot's downsampling blurs the dense modules and decoding fails. Measured on a ~487KB text file: 235 codes (chunk-size 400) vs 53 codes (`chunk-size 1800`).

**Three encoder defaults are resolved *after* `--backend` is parsed, so they differ per symbology** (`encoder.py` leaves them unset and fills them in): `--chunk-size` 800 → 3000, `--cols` 6 → 1, `--qr-size` 180 → 900 when `--backend jab`. Passing any of them explicitly wins over the backend default.

JAB Code's ceiling is higher because raw payloads skip base85 and color multiplies the bits per module. A single basic symbol runs Version 1..32 with side length `4 × version + 17` modules, i.e. up to `145 × 145`. At 8 colors / ECC level 3, Version 32 holds roughly **4245 bytes**, so minus the 12-byte header the practical max is `--chunk-size ~4233`; on a 1080-pixel-tall screen the largest integer module size is `floor(1080 / 145) = 7`. For pixel-perfect screenshots, `--chunk-size 4000 --jab-module-size 7` is the near-ceiling setting; go conservative for camera shots. See `jabcode-chat-record.md` for the full derivation.

### The chunk binary format is the load-bearing contract

Every chunk is `MAGIC(2) | index(2,>H) | total(2,>H) | datalen(2,>H) | checksum(4) | data(N)` — a 12-byte header where `checksum = sha256(data)[:4]`. This struct is **hand-duplicated, not shared**, across six places:

- `queqiao/encoder.py` → `encode_chunks()` (writer)
- `queqiao/decoder.py` → `decode_single_chunk()`
- `tests/test_roundtrip.py`, `tests/test_qr_roundtrip.py`, `tests/verify_full.py` (inline parsers)

**If you change the header layout, magic, checksum, or struct format, you must update all of these in lockstep.** `make_diff.py` does **not** touch the wire format and is not part of this set. Neither is `tests/test_jab_backend.py` — it imports `decode_single_chunk` rather than reimplementing the parser, so the count stays at five. (The legacy `decode_pyzbar.py` copy was deleted when the project became a package.)

**Chunk index 0 is a metadata chunk** whose payload is compact JSON:
`{"version":1,"filename":"input.txt","size":12345,"sha256":"abcdef...","compressed_size":5678}`
Data chunks occupy indices 1..N. The `total` field in every header = N+1 (metadata + data). Decoders extract metadata from index 0, merge only `range(1, total)` for data, then verify the whole-file SHA256 after decompression. If index 0 is missing, decoders warn but proceed without verification.

### Reassembly is order-independent by design

The `index`/`total` fields in the header — not image position — drive reconstruction. Decoders dedupe by index, drop chunks failing the SHA256 check, and abort listing any missing indices. Consequence: `decoder.py`'s `sort_qr_by_position()` is essentially cosmetic. Multi-photo decode works by concatenating all detected codes across all images into one pool.

This is also what makes the jab backend workable despite its one-code-per-image reader: N cropped single-code PNGs pool into the same index-keyed reassembly as one photo containing N QR codes.

### Decoder backends live in `src/queqiao/qr_backends/`

Backends are pluggable. Each is one module that subclasses `queqiao.qr_backends.base.QRDecoderAdapter` and implements `decode_image(path) -> list[QRDecodeResult]`. Registration is explicit in `qr_backends/__init__.py` — the dict order defines `available_backends()` order, and `DEFAULT_BACKEND` is the CLI default. `decoder.py` only talks to the registry (`get_backend(name)`); it does not know which backends exist.

The adapter contract has **two** class attributes, not one: `name` (the `--backend` identifier) and `payload_encoding` (`'base85'` by default, `'raw'` for symbologies that carry bytes directly). A backend that returns raw chunk bytes **must** override `payload_encoding` or the header parse will fail on garbage. Missing third-party/native deps should be raised as `RuntimeError` from `__init__` so the CLI can print an install hint.

To add a new backend: write `src/queqiao/qr_backends/<name>_backend.py`, then import + register it in `src/queqiao/qr_backends/__init__.py`. The decoder CLI's `--backend` choices update automatically. **Encoding is not symmetric** — adding an encode-side symbology means touching `queqiao/encoder.py`'s hard-coded `--backend` choices and `generate_html()` branch as well.

Currently shipped:
- **zxing** (`--backend zxing`, **default**): pure-wheel C++ port of ZXing (`pip install zxing-cpp`) — **no native system library to install**. Reads PIL images directly and exposes raw payload bytes via `barcode.bytes` (no utf-8 round-trip), and gives a 4-corner `position` we map to a bounding box. On pixel-perfect screenshot transfer (queqiao's primary use case) it is ~10–14× faster than pyzbar at 100% accuracy across chunk-sizes 800/1500/1800; on degraded photos (downsample + Gaussian blur + JPEG) the crash threshold is the same as pyzbar — neither offers a robustness edge on dense v40-class codes once downsampling drops below ~30% with blur. See `tests/bench_backends.py` for the reproducible benchmark.
- **pyzbar** (`--backend pyzbar`): opt-in legacy backend that wraps the **native zbar library** (`brew install zbar`; macOS also needs `DYLD_LIBRARY_PATH=/opt/homebrew/lib`, which `run.sh` sets). Kept as a fallback for specific samples where zxing fails to detect — currently no such samples are documented.
- **jab** (`--backend jab`, `payload_encoding='raw'`): color barcode (JAB Code) for much denser transfer. This backend is a **subprocess bridge to the official reference CLI**, not a library — `jabcode_cli.py` shells out to `jabcodeWriter` (encode) and `jabcodeReader` (decode). Those binaries are **not** Python packages and `uv sync` will not provide them: build them from <https://github.com/jabcode/jabcode> and put them on `PATH`, or point `QUEQIAO_JAB_WRITER` / `QUEQIAO_JAB_READER` at them. Two constraints follow from the reference reader: it accepts PNG/TIFF only (the backend normalizes camera formats to PNG in a temp dir first), and **it decodes exactly one JAB Code per image** — so `decode_image()` always returns 0 or 1 results, and a full-page screenshot of many codes must be cropped into per-code images before decoding. That is why the encoder defaults to `--cols 1` for jab.

Switch to pyzbar only when you have a concrete sample that zxing cannot decode. Reach for jab when you need the density and can afford building the native tools plus the per-code cropping step.

### Directory comparison gotchas (`make_diff.py`)

- **Argument order is the diff direction**: `dir1` = base (external), `dir2` = target (internal). Diff is dir1→dir2, applied with `patch -p1`. Swapping the args silently produces a reversed patch.
- **All dotfiles/dirs are skipped unconditionally** in `collect_files()` (anything starting with `.`) — this happens *before* gitignore is consulted, so `.gitignore`, `.env`, etc. are never diffed regardless of flags.
- **gitignore support is partial**: negation patterns (`!`) are explicitly ignored; matching is a hand-rolled `fnmatch` approximation, not full gitignore semantics. `.gitignore` files from *both* dirs are merged.
- Binary/undecodable files (utf-8 read fails) are counted and skipped, never diffed.
