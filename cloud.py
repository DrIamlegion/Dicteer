#!/usr/bin/env python3
"""Optional online transcription through Groq's free Whisper tier.

The app is offline by default. When the user explicitly switches the online
backend on, only the finished utterance is uploaded - never the live preview -
and the text comes back through the same post-processing and delivery path as a
local transcript. Any failure falls back to the local model instead of losing
the dictation.

API keys are encrypted with the Windows Data Protection API (DPAPI) and stored
next to ``config.json``. The key is never written to the config, the log or the
window; the bridge only reports whether one is present.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import logging
import os
import threading
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("dictate")

PROVIDER = "groq"
GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_KEYS_URL = "https://console.groq.com/keys"
FREE_LIMITS = "20 verzoeken/min · 2.000/dag · 7.200 audio-seconden/uur"


class CloudError(RuntimeError):
    """A user-readable reason the online transcript was not used."""


class CredentialError(RuntimeError):
    """The key could not be stored or read safely."""


# --------------------------------------------------------------------------
# Windows DPAPI
# --------------------------------------------------------------------------
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wt.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    if not data:
        raise CredentialError("De sleutel is leeg.")
    buffer = ctypes.create_string_buffer(data, len(data))
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))),
        buffer,
    )


def _crypt32():
    if os.name != "nt":
        raise CredentialError("Sleutels worden alleen op Windows ondersteund.")
    return ctypes.windll.crypt32


def _local_free(pointer) -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))


def protect(plain: str) -> str:
    """Encrypt a secret for the current Windows user, returned as base64."""
    data = str(plain).encode("utf-8")
    in_blob, _keep = _blob(data)
    out_blob = _DataBlob()
    crypt32 = _crypt32()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Dicteer",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _local_free(out_blob.pbData)
    return base64.b64encode(encrypted).decode("ascii")


def unprotect(token: str) -> str:
    """Decrypt a value produced by :func:`protect` for this Windows user."""
    try:
        data = base64.b64decode(str(token), validate=True)
    except Exception as exc:
        raise CredentialError("De opgeslagen sleutel is beschadigd.") from exc
    in_blob, _keep = _blob(data)
    out_blob = _DataBlob()
    crypt32 = _crypt32()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise CredentialError(
            "De sleutel kan niet worden ontsleuteld. Sla hem opnieuw op."
        )
    try:
        plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _local_free(out_blob.pbData)
    return plain.decode("utf-8")


# --------------------------------------------------------------------------
# key store
# --------------------------------------------------------------------------
class CredentialStore:
    """One encrypted entry per provider, stored beside the config.

    The file itself is not secret: DPAPI binds every value to the Windows user
    who saved it, so copying the file to another account does not reveal it.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.warning("credentials file is unreadable: %s", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:
            raise CredentialError(f"De sleutel kon niet worden bewaard: {exc}") from exc

    @staticmethod
    def _provider(provider: str) -> str:
        name = str(provider or "").strip().lower()
        if not name or not all(ch.isalnum() or ch in "-_" for ch in name):
            raise CredentialError("Onbekende provider.")
        return name

    def has(self, provider: str = PROVIDER) -> bool:
        """True only when the stored value can actually be decrypted."""
        try:
            self.get(provider)
        except Exception as exc:
            log.debug("stored key for %s is unusable: %s", provider, exc)
            return False
        return True

    def get(self, provider: str = PROVIDER) -> str:
        token = self._read().get(self._provider(provider))
        if not token:
            raise CredentialError("Er is nog geen sleutel opgeslagen.")
        return unprotect(token)

    def set(self, key: str, provider: str = PROVIDER) -> None:
        name = self._provider(provider)
        clean = str(key or "").strip()
        if not clean:
            raise CredentialError("De sleutel is leeg.")
        if len(clean) < 20:
            raise CredentialError("Dit lijkt geen geldige API-sleutel.")
        token = protect(clean)
        with self._lock:
            data = self._read()
            data[name] = token
            self._write(data)
        log.info("stored an encrypted API key for %s", name)

    def clear(self, provider: str = PROVIDER) -> bool:
        name = self._provider(provider)
        with self._lock:
            data = self._read()
            removed = data.pop(name, None) is not None
            if removed:
                self._write(data)
        if removed:
            log.info("removed the API key for %s", name)
        return removed


# --------------------------------------------------------------------------
# Groq transcription
# --------------------------------------------------------------------------
def wav_bytes(audio, sample_rate: int = 16000) -> bytes:
    """16-bit mono WAV from the float32 array the recorder produces."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not samples.size:
        raise CloudError("De opname is leeg.")
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767.0).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm)
    return buffer.getvalue()


def _multipart(fields: dict[str, str], filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = f"----Dicteer{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        if value is None or value == "":
            continue
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(payload)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


class CloudTranscriber:
    """POST a finished utterance to Groq and return its text.

    ``url`` is injectable so the self-test can run a local HTTP server instead
    of sending audio to Groq.
    """

    def __init__(
        self,
        credentials: CredentialStore,
        url: str = GROQ_URL,
        model: str = GROQ_MODEL,
        timeout: float = 30.0,
    ):
        self.credentials = credentials
        self.url = url
        self.model = model
        self.timeout = float(timeout)

    def transcribe(
        self,
        audio,
        language: str | None = None,
        prompt: str | None = None,
        timeout: float | None = None,
    ) -> str:
        try:
            key = self.credentials.get(PROVIDER)
        except CredentialError as exc:
            raise CloudError(str(exc)) from exc

        fields = {
            "model": self.model,
            "response_format": "json",
            "language": (language or "").strip(),
            "prompt": (prompt or "").strip(),
        }
        payload = wav_bytes(audio)
        body, boundary = _multipart(fields, "dictation.wav", payload)
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
                "User-Agent": "Dicteer/2.0",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=float(timeout or self.timeout)
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise CloudError(self._http_message(exc)) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise CloudError(f"Geen verbinding met Groq ({reason}).") from None
        except TimeoutError:
            raise CloudError("Groq reageerde niet op tijd.") from None

        try:
            data = json.loads(raw.decode("utf-8"))
            text = str(data.get("text") or "").strip()
        except Exception as exc:
            raise CloudError("Groq gaf een onleesbaar antwoord.") from exc
        if not text:
            raise CloudError("Groq herkende geen spraak in de opname.")
        return " ".join(text.split())

    @staticmethod
    def _http_message(exc: urllib.error.HTTPError) -> str:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
                detail = str(
                    (parsed.get("error") or {}).get("message")
                    if isinstance(parsed.get("error"), dict)
                    else parsed.get("error") or ""
                )
            except Exception:
                detail = raw
        except Exception:
            pass
        detail = " ".join(detail.split())[:240]
        if exc.code == 401:
            return "De Groq-sleutel is ongeldig. Controleer hem in Instellingen."
        if exc.code == 403:
            return "Groq weigert deze sleutel of dit model."
        if exc.code == 413:
            return "De opname is te groot voor Groq."
        if exc.code == 429:
            return "De gratis limiet van Groq is bereikt. Probeer het later opnieuw."
        suffix = f" {detail}" if detail else ""
        return f"Groq gaf HTTP {exc.code}.{suffix}".strip()
