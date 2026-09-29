"""The Writer: a small language model run by the embedded llama.cpp engine.

Loaded only when needed and unloaded after a few idle minutes to free RAM.
"""
from __future__ import annotations

import atexit
import hashlib
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from typing import Iterator

from . import config

IDLE_UNLOAD_SECONDS = 300
CONTEXT_TOKENS = 4096


class Writer:
    def __init__(self):
        self._llm = None
        self._spec: config.WriterSpec | None = None
        self._lock = threading.Lock()          # one generation at a time
        self._last_used = 0.0
        self.interactive_waiting = 0           # background work yields to users
        self.download = {"active": False, "key": None, "done": 0, "total": 0, "error": None}
        config.move_legacy_brains()
        threading.Thread(target=self._idle_watch, daemon=True).start()
        # Free the model before Python shuts down; otherwise llama.cpp's Metal
        # backend can abort while the process exits.
        atexit.register(self._unload)

    def _unload(self):
        self._llm = None

    # ---------- status ----------
    @property
    def spec(self) -> config.WriterSpec | None:
        """The brain Folaio will use (a newly downloaded one takes over on the next question)."""
        return config.active_writer()

    @property
    def engine_installed(self) -> bool:
        """Is the optional llama.cpp engine installed (requirements-plus.txt)?"""
        import importlib.util
        return importlib.util.find_spec("llama_cpp") is not None

    @property
    def available(self) -> bool:
        """A Plus brain is downloaded, switched on, and the engine to run it is installed."""
        return self.spec is not None and self.engine_installed

    @property
    def loaded(self) -> bool:
        return self._llm is not None

    # ---------- loading ----------
    def _ensure_loaded(self):
        spec = config.active_writer()
        if spec is None:
            raise RuntimeError("No brain installed yet. Download one from the Brain panel.")
        if self._llm is not None and self._spec == spec:
            return
        from llama_cpp import Llama
        self._llm = None
        self._llm = Llama(
            model_path=str(spec.path),
            n_ctx=CONTEXT_TOKENS,
            n_threads=max(1, config.hardware()["cpu_cores"] - 1),
            n_gpu_layers=-1,   # uses Apple Metal on Macs; ignored on CPU-only builds
            verbose=False,
        )
        self._spec = spec

    def _idle_watch(self):
        while True:
            time.sleep(30)
            if self._llm is not None and time.time() - self._last_used > IDLE_UNLOAD_SECONDS:
                with self._lock:
                    if time.time() - self._last_used > IDLE_UNLOAD_SECONDS:
                        self._llm = None

    # ---------- generation ----------
    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.2,
             json_schema: dict | None = None, background: bool = False) -> str:
        return "".join(self.stream(messages, max_tokens, temperature, json_schema, background))

    def stream(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.2,
               json_schema: dict | None = None, background: bool = False) -> Iterator[str]:
        if background:
            while self.interactive_waiting:   # let the user go first
                time.sleep(0.5)
            self._lock.acquire()
        else:
            self.interactive_waiting += 1
            try:
                self._lock.acquire()
            finally:
                self.interactive_waiting -= 1
        try:
            self._ensure_loaded()
            kwargs = dict(messages=messages, max_tokens=max_tokens,
                          temperature=temperature, stream=True)
            if json_schema:
                # Grammar-constrained output: even tiny models return valid JSON.
                kwargs["response_format"] = {"type": "json_object", "schema": json_schema}
            for part in self._llm.create_chat_completion(**kwargs):
                piece = part["choices"][0]["delta"].get("content")
                if piece:
                    self._last_used = time.time()
                    yield piece
            self._last_used = time.time()
        finally:
            self._lock.release()

    # ---------- downloading ----------
    def start_download(self, key: str):
        spec = config.writer_by_key(key)
        if self.download["active"] or spec.path.exists():
            return   # already downloading, or already installed
        try:
            config.models_dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            self.download = {"active": False, "key": key, "done": 0, "total": spec.size_bytes,
                             "error": "The brain folder isn't available. Is the drive connected? "
                                      "You can choose another folder above."}
            return
        self.download = {"active": True, "key": key, "done": 0, "total": spec.size_bytes, "error": None}
        threading.Thread(target=self._download, args=(spec,), daemon=True).start()

    def _download(self, spec: config.WriterSpec, attempts: int = 5):
        """Download with resume. A stalled or dropped connection is retried
        automatically, continuing from where it stopped."""
        part = spec.path.with_suffix(".part")
        try:
            for attempt in range(1, attempts + 1):
                try:
                    self._fetch(spec, part)
                    break
                except (OSError, TimeoutError) as e:
                    if attempt == attempts:
                        raise IOError("Couldn't finish the download. Check your internet "
                                      "connection and try again; it will continue where it stopped.") from e
                    self.download["error"] = f"Connection problem, retrying ({attempt}/{attempts - 1})…"
                    time.sleep(3 * attempt)
            self.download["error"] = None
            self.download["verifying"] = True
            if _sha256(part) != spec.sha256:
                part.unlink(missing_ok=True)   # damaged or altered: never keep it
                raise IOError("The downloaded brain didn't match its official fingerprint, so it was "
                              "deleted for your safety. Please try downloading again.")
            part.rename(spec.path)
        except Exception as e:  # surfaced in the UI
            self.download["error"] = str(e)
        finally:
            self.download["active"] = False
            self.download["verifying"] = False

    def _fetch(self, spec: config.WriterSpec, part):
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(spec.url, headers={"Range": f"bytes={have}-"} if have else {})
        # 20 s without any data counts as a stalled connection.
        with urllib.request.urlopen(req, timeout=20) as resp, open(part, "ab" if have else "wb") as out:
            if have and resp.status != 206:   # server ignored resume: start over
                out.truncate(0)
                have = 0
            self.download["done"] = have
            self.download["error"] = None
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                self.download["done"] += len(chunk)
        if part.stat().st_size != spec.size_bytes:
            raise IOError("download incomplete")

    def remove(self, key: str):
        """Delete a downloaded brain to free disk space."""
        spec = config.writer_by_key(key)
        with self._lock:
            if self._spec == spec:
                self._llm, self._spec = None, None
            spec.path.unlink(missing_ok=True)
            spec.path.with_suffix(".part").unlink(missing_ok=True)

    def move_to(self, folder: Path | None):
        """Keep brains in another folder (None = the default). Downloaded brains move along."""
        if self.download["active"]:
            raise ValueError("Please wait for the current download to finish first.")
        new = (folder.expanduser() if folder else config.DEFAULT_MODELS_DIR).resolve()
        if not new.is_absolute():
            raise ValueError("Please choose a full folder path.")
        try:
            new.mkdir(parents=True, exist_ok=True)
            probe = new / ".folaio-write-test"
            probe.write_text("ok")
            probe.unlink()
        except OSError:
            raise ValueError("Folaio can't save files in that folder. Please choose another one.")
        old = config.models_dir()
        if old.exists() and new == old.resolve():
            return
        have = config.downloaded_writers()
        need = sum(w.path.stat().st_size for w in have)
        if shutil.disk_usage(new).free < need * 1.05 + 50e6:
            raise ValueError("There isn't enough free space in that folder for your brains.")
        with self._lock:
            self._llm, self._spec = None, None   # release the file before moving it
            for w in have:
                shutil.move(str(w.path), str(new / w.file))
            for part in old.glob("*.part") if old.is_dir() else []:
                part.unlink(missing_ok=True)       # unfinished downloads start again there
            default = config.DEFAULT_MODELS_DIR.resolve()
            config.save_settings(models_dir=None if new == default else str(new))

    def switched(self):
        """The user picked another brain (or Off): free the old one's memory now."""
        with self._lock:
            if self._spec != config.active_writer():
                self._llm, self._spec = None, None


def _sha256(path: Path) -> str:
    """The file's SHA-256 fingerprint (read in 4 MB pieces, so memory stays small)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()
