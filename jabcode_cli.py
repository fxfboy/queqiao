"""Helpers for invoking the official JAB Code reference command-line tools."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


JABCODE_REPOSITORY = "https://github.com/jabcode/jabcode"


def find_executable(kind):
    """Find jabcodeWriter/jabcodeReader via an override or PATH."""
    names = {
        "writer": ("QUEQIAO_JAB_WRITER", "jabcodeWriter"),
        "reader": ("QUEQIAO_JAB_READER", "jabcodeReader"),
    }
    env_name, command = names[kind]
    override = os.environ.get(env_name)
    candidate = override or shutil.which(command)
    if candidate and Path(candidate).is_file():
        return str(Path(candidate).resolve())
    raise RuntimeError(
        f"JAB Code {kind} is unavailable. Build the official reference tools from "
        f"{JABCODE_REPOSITORY}, put {command} on PATH, or set {env_name}."
    )


def run_writer(payload, output_path, colors=8, module_size=12, ecc_level=3,
               executable=None):
    """Encode binary payload into a JAB Code PNG using jabcodeWriter."""
    writer = executable or find_executable("writer")
    output_path = Path(output_path)
    with tempfile.TemporaryDirectory(prefix="queqiao-jab-write-") as temp_dir:
        input_path = Path(temp_dir) / "payload.bin"
        input_path.write_bytes(payload)
        command = [
            writer,
            "--input-file", str(input_path),
            "--output", str(output_path),
            "--color-number", str(colors),
            "--module-size", str(module_size),
            "--ecc-level", str(ecc_level),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not output_path.is_file():
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"jabcodeWriter failed with exit code {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    return output_path


def run_reader(image_path, executable=None):
    """Decode one JAB Code image and return its exact binary payload."""
    reader = executable or find_executable("reader")
    with tempfile.TemporaryDirectory(prefix="queqiao-jab-read-") as temp_dir:
        output_path = Path(temp_dir) / "payload.bin"
        command = [reader, str(image_path), "--output", str(output_path)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not output_path.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise ValueError(
                f"jabcodeReader could not decode {image_path} "
                f"(exit code {result.returncode})"
                + (f": {detail}" if detail else "")
            )
        return output_path.read_bytes()
