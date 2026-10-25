#!/usr/bin/env python3
"""Optional online transcription through Groq or Google Gemini.

The app is offline by default. When the user explicitly switches the online
backend on, only the finished utterance is uploaded - never the live preview -
and the text comes back through the same post-processing and delivery path as a
local transcript. Any failure falls back to the local model instead of losing
the dictation.

Groq is Whisper on someone else's GPU: one multipart POST with the clip and a
biasing prompt. Gemini has no such endpoint, so one generateContent call carries
the clip inline plus an instruction to transcribe it. Both engines answer with
the same plain text, which is all :class:`CloudTranscriber` exposes.

API keys are encrypted with the Windows Data Protection API (DPAPI) and stored
next to ``config.json``, one entry per provider. A key is never written to the
config, the log or the window; the bridge only reports whether one is present.
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

# Measured on the same 5.4 s clip, with the vocabulary prompt: flash-lite
# answered in 1.3 s, 3.6 Flash in 5.2 s with better punctuation, and 3.8 Flash
# in 31 s and once over two minutes - too slow for the 30 s dictation timeout.
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_MODELS = (
    {
        "value": "gemini-3.5-flash-lite",
        "label": "3.5 Flash-Lite",
        "note": "snelst, ongeveer 1 seconde per dictaat",
    },
    {
        "value": "gemini-3.6-flash",
        "label": "3.6 Flash",
        "note": "nauwkeuriger en betere leestekens, ongeveer 5 seconden",
    },
)
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_KEYS_URL = "https://aistudio.google.com/app/apikey"
GEMINI_LIMITS = "Gratis tier · daglimiet en snelheid verschillen per model (zie AI Studio)"

USER_AGENT = "Dicteer/2.0.11"

# Every online provider the app can talk to. The window only ever sees these
# fields, never a key: ``status`` copies them out for the settings card.
PROVIDERS = {
    "groq": {
        "name": "Groq",
        "model": GROQ_MODEL,
        "free_limits": FREE_LIMITS,
        "keys_url": GROQ_KEYS_URL,
        "key_hint": "gsk_...",
    },
    "gemini": {
        "name": "Google Gemini",
        "model": GEMINI_MODEL,
        "free_limits": GEMINI_LIMITS,
        "keys_url": GEMINI_KEYS_URL,
        "key_hint": "AIza...",
    },
}

# Gemini gets an instruction rather than Whisper's biasing prompt, so the
# language code is spelled out as a name it can act on.
LANGUAGE_NAMES = {
    "en": "English",
    "nl": "Dutch",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
}


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


def _http_detail(exc: urllib.error.HTTPError) -> str:
    """The provider's own one-line explanation, when it sent one."""
    detail = ""
    try:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
            error = parsed.get("error")
            detail = str(error.get("message") if isinstance(error, dict) else error or "")
        except Exception:
            detail = raw
    except Exception:
        pass
    return " ".join(detail.split())[:240]


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


class GroqTranscriber:
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
                "User-Agent": USER_AGENT,
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
        detail = _http_detail(exc)
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
# --------------------------------------------------------------------------
# Gemini transcription
# --------------------------------------------------------------------------
class GeminiTranscriber:
    """One generateContent call per finished utterance.

    Gemini has no Whisper-style transcription endpoint: a short instruction and
    the clip travel together, and the model answers with the transcript alone.
    ``url`` is injectable so the self-test can point at a local HTTP server.
    """

    def __init__(
        self,
        credentials: CredentialStore,
        url: str | None = None,
        model: str = GEMINI_MODEL,
        timeout: float = 30.0,
    ):
        self.credentials = credentials
        self.model = model
        self.url = url or GEMINI_BASE.format(model=model)
        self.timeout = float(timeout)

    def transcribe(
        self,
        audio,
        language: str | None = None,
        prompt: str | None = None,
        timeout: float | None = None,
    ) -> str:
        try:
            key = self.credentials.get("gemini")
        except CredentialError as exc:
            raise CloudError(str(exc)) from exc

        body = json.dumps(
            {
                "contents": [
                    {
                        "parts": [
                            {"text": self._instruction(language, prompt)},
                            {
                                "inline_data": {
                                    "mime_type": "audio/wav",
                                    "data": base64.b64encode(wav_bytes(audio)).decode("ascii"),
                                }
                            },
                        ]
                    }
                ],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "x-goog-api-key": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
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
            raise CloudError(f"Geen verbinding met Gemini ({reason}).") from None
        except TimeoutError:
            raise CloudError("Gemini reageerde niet op tijd.") from None

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise CloudError("Gemini gaf een onleesbaar antwoord.") from exc
        return " ".join(self._transcript(data).split())

    @staticmethod
    def _instruction(language: str | None, vocabulary: str | None) -> str:
        """Ask for the words, not a summary - and for the user's own spelling."""
        text = (
            "Transcribe the speech in this audio verbatim. Keep the speaker's "
            "words, casing and punctuation. Return only the transcript: no "
            "quotes, no introduction, no translation, no commentary."
        )
        name = LANGUAGE_NAMES.get(str(language or "").strip().lower())
        if name:
            text += f" The speech is in {name}."
        else:
            text += " Detect the language yourself and transcribe in it."
        words = " ".join(str(vocabulary or "").split())
        if words:
            text += f" Spell these names and terms exactly like this: {words}."
        return text

    @staticmethod
    def _transcript(data: dict) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            blocked = str(feedback.get("blockReason") or "").strip()
            raise CloudError(
                f"Gemini weigerde de opname ({blocked})."
                if blocked
                else "Gemini herkende geen spraak in de opname."
            )
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        text = "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict)
        ).strip()
        if not text:
            reason = str(candidate.get("finishReason") or "").strip()
            detail = f" ({reason})" if reason else ""
            raise CloudError(f"Gemini gaf geen transcriptie terug{detail}.")
        return text

    @staticmethod
    def _http_message(exc: urllib.error.HTTPError) -> str:
        detail = _http_detail(exc)
        suffix = f" {detail}" if detail else ""
        if exc.code == 400:
            if "api key" in detail.lower():
                return "De Gemini-sleutel is ongeldig. Controleer hem in Instellingen."
            return f"Gemini weigerde het verzoek.{suffix}".strip()
        if exc.code in (401, 403):
            return "Google weigert deze sleutel of dit model."
        if exc.code == 404:
            return "Dit Gemini-model is niet beschikbaar voor deze sleutel."
        if exc.code == 413:
            return "De opname is te groot voor Gemini."
        if exc.code == 429:
            return "De gratis limiet van Gemini is bereikt. Probeer het later opnieuw."
        if exc.code in (500, 502, 503):
            return "Gemini is even niet beschikbaar. Probeer het zo opnieuw."
        return f"Gemini gaf HTTP {exc.code}.{suffix}".strip()


# --------------------------------------------------------------------------
# the backend the user picked
# --------------------------------------------------------------------------
class CloudTranscriber:
    """The selected online provider behind one small interface.

    ``dictate.py`` only calls ``transcribe`` and reads ``model``; which company
    answers follows ``online_backend``. ``use`` switches without a restart, so
    the window can move between providers as soon as the setting is saved.
    """

    def __init__(
        self,
        credentials: CredentialStore,
        backend: str = PROVIDER,
        url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        gemini_model: str | None = None,
    ):
        self.credentials = credentials
        self.timeout = float(timeout)
        self._url = url
        self._model = model
        self._gemini_model = gemini_model or GEMINI_MODEL
        self._engine = None
        self.backend = ""
        self.use(backend or PROVIDER)

    def _model_for(self, provider: str) -> str:
        if self._model:
            return self._model
        if provider == "gemini":
            return self._gemini_model
        return PROVIDERS[provider]["model"]

    def use(self, backend: str, gemini_model: str | None = None) -> "CloudTranscriber":
        """Follow the chosen backend, and the Gemini model when one is picked."""
        if gemini_model:
            self._gemini_model = str(gemini_model)
        name = str(backend or "").strip().lower()
        if name not in PROVIDERS:
            return self
        wanted = self._model_for(name)
        if (
            name == self.backend
            and self._engine is not None
            and getattr(self._engine, "model", None) == wanted
        ):
            return self
        kind = GeminiTranscriber if name == "gemini" else GroqTranscriber
        self._engine = kind(
            self.credentials,
            url=self._url,
            model=wanted,
            timeout=self.timeout,
        )
        self.backend = name
        return self

    @property
    def model(self) -> str:
        return self._model_for(self.backend)

    def transcribe(
        self,
        audio,
        language: str | None = None,
        prompt: str | None = None,
        timeout: float | None = None,
    ) -> str:
        return self._engine.transcribe(
            audio, language=language, prompt=prompt, timeout=timeout
        )


def status(
    credentials: CredentialStore, backend: str = "local", gemini_model: str | None = None
) -> dict:
    """What the window may know: which providers exist and who has a key.

    The key itself never leaves this module; the settings card only learns
    whether one can be decrypted.
    """
    chosen = str(backend or "local").strip().lower()
    if chosen not in PROVIDERS:
        chosen = "local"
    keys = {}
    for name in PROVIDERS:
        try:
            keys[name] = bool(credentials.has(name))
        except Exception as exc:  # a damaged credential file counts as "no key"
            log.debug("could not check the %s key: %s", name, exc)
            keys[name] = False
    meta = PROVIDERS.get(chosen, {})
    picked = str(gemini_model or "").strip()
    if picked not in [entry["value"] for entry in GEMINI_MODELS]:
        picked = GEMINI_MODEL
    # The card shows the provider's own model next to a verdict; for Gemini that
    # is whichever one the user picked, not the constant default.
    providers = {
        name: {
            field: PROVIDERS[name][field]
            for field in ("name", "model", "free_limits", "keys_url", "key_hint")
        }
        for name in PROVIDERS
    }
    providers["gemini"]["model"] = picked
    return {
        "backend": chosen,
        "provider": chosen if chosen != "local" else PROVIDER,
        "name": meta.get("name", "Lokaal (Whisper)"),
        "model": picked if chosen == "gemini" else meta.get("model", ""),
        "gemini_model": picked,
        "gemini_models": [dict(entry) for entry in GEMINI_MODELS],
        "free_limits": meta.get("free_limits", ""),
        "keys_url": meta.get("keys_url", ""),
        "key_hint": meta.get("key_hint", ""),
        "has_key": bool(keys.get(chosen, False)),
        "keys": keys,
        "providers": providers,
    }
