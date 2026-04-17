"""
Text-to-Speech helper.
Primary: edge-tts (high-quality Microsoft neural voices via internet)
Fallback: Windows SAPI5 via PowerShell (offline, robotic)

Caches generated MP3 files so repeated playback is instant.
"""

import hashlib
import os
import subprocess
import threading

from app_path import app_dir

TTS_CACHE_DIR = os.path.join(app_dir(), "tts_cache")
EDGE_VOICE = "en-US-AriaNeural"  # Neural voice, natural sounding


def _ensure_cache_dir():
    try:
        os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    except OSError:
        pass


def _cache_path(text: str, voice: str) -> str:
    key = f"{voice}:{text}".encode("utf-8")
    digest = hashlib.md5(key).hexdigest()
    return os.path.join(TTS_CACHE_DIR, f"{digest}.mp3")


def _generate_edge_tts(text: str, out_path: str, voice: str = EDGE_VOICE) -> bool:
    """Generate MP3 using edge-tts (requires internet).
    Returns True on success."""
    try:
        import asyncio
        import edge_tts  # type: ignore

        async def _gen():
            comm = edge_tts.Communicate(text, voice)
            await comm.save(out_path)

        try:
            asyncio.run(_gen())
        except RuntimeError:
            # Event loop already running — create new one
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_gen())
            finally:
                loop.close()
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception as e:
        print(f"[TTS] edge-tts failed: {e}")
        return False


def _play_sapi5(text: str):
    """Fallback: use Windows SAPI5 via PowerShell (offline, robotic)."""
    ps_script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Speak('{text.replace(chr(39), chr(39)*2)}')"
    )
    try:
        subprocess.Popen(
            ["powershell", "-Command", ps_script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        print(f"[TTS] SAPI5 fallback failed: {e}")


def _play_mp3(path: str):
    """Play an MP3 file using pygame.mixer (already used for fire_alert.mp3)."""
    try:
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        # Use a separate Sound object so it doesn't interrupt other music
        sound = pygame.mixer.Sound(path)
        sound.play()
    except Exception as e:
        print(f"[TTS] Playback failed: {e}")


def speak(text: str, voice: str = EDGE_VOICE):
    """Speak the given text (non-blocking).
    Tries edge-tts first (natural voice), falls back to SAPI5 if offline."""
    _ensure_cache_dir()
    cache_file = _cache_path(text, voice)

    def _run():
        # Use cached file if available
        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
            _play_mp3(cache_file)
            return

        # Try edge-tts first
        if _generate_edge_tts(text, cache_file, voice):
            _play_mp3(cache_file)
            return

        # Fallback to SAPI5
        print(f"[TTS] Using SAPI5 fallback for: {text!r}")
        _play_sapi5(text)

    threading.Thread(target=_run, daemon=True).start()


def pregenerate(phrases: list[str], voice: str = EDGE_VOICE):
    """Pre-generate TTS for a list of phrases so they play instantly later.
    Runs in background; silently fails if edge-tts isn't available."""
    _ensure_cache_dir()

    def _run():
        for text in phrases:
            cache_file = _cache_path(text, voice)
            if not os.path.exists(cache_file):
                _generate_edge_tts(text, cache_file, voice)

    threading.Thread(target=_run, daemon=True).start()
