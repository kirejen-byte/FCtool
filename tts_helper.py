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

from app_path import app_dir, bundle_dir

TTS_CACHE_DIR = os.path.join(app_dir(), "tts_cache")
EDGE_VOICE = "en-US-ChristopherNeural"  # Deep male neural voice (matches bundled cues)

# Safety-critical fixed cues ship as PRE-RENDERED clips so they never depend on a
# live edge-tts call at alert time (Microsoft rotates the read-aloud handshake,
# which silently degrades runtime TTS to robotic SAPI — see tools/gen_tts_assets.py).
# Keys are the exact phrase strings fc_gui passes to speak()/pregenerate(); they
# must match byte-for-byte (tests/test_tts.py guards this). Paths are relative to
# the bundle root and resolved via _bundled_clip_path().
BUNDLED_CLIPS = {
    "Ten percent of fleet lost": os.path.join("assets", "tts", "fleet_lost_10.mp3"),
    "Twenty five percent of fleet lost": os.path.join("assets", "tts", "fleet_lost_25.mp3"),
    "Fifty percent of fleet lost": os.path.join("assets", "tts", "fleet_lost_50.mp3"),
    "Decloaked": os.path.join("assets", "tts", "decloaked.mp3"),
}


def _bundled_clip_path(text: str) -> str | None:
    """Return an existing on-disk path to the pre-rendered clip for `text`, or
    None if `text` has no bundled clip (or the file is missing).

    Mirrors how fire_alert.mp3 is resolved in fc_gui (_play_fire_alert): try the
    writable app dir first, then the read-only bundle dir (sys._MEIPASS in the
    frozen onefile exe). This is independent of any `voice` argument — the clip
    IS the voice."""
    rel = BUNDLED_CLIPS.get(text)
    if not rel:
        return None
    for base in (app_dir(), bundle_dir()):
        cand = os.path.join(base, rel)
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            return cand
    return None


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

    Resolution order:
      1. Bundled pre-rendered clip (if `text` is a fixed cue and the file
         exists) — no network, always the intended voice.
      2. Cached MP3 from a previous edge-tts generation.
      3. edge-tts (natural voice, requires internet).
      4. Windows SAPI5 fallback (offline, robotic).
    """
    _ensure_cache_dir()
    cache_file = _cache_path(text, voice)

    def _run():
        # Bundled fixed-cue clip wins (voice-independent; the clip IS the voice)
        bundled = _bundled_clip_path(text)
        if bundled:
            _play_mp3(bundled)
            return

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
    Runs in background; silently fails if edge-tts isn't available.

    Phrases that ship as bundled clips are skipped entirely (no network call,
    no cache write) — speak() plays their bundled file directly."""
    _ensure_cache_dir()

    def _run():
        for text in phrases:
            if _bundled_clip_path(text):
                continue
            cache_file = _cache_path(text, voice)
            if not os.path.exists(cache_file):
                _generate_edge_tts(text, cache_file, voice)

    threading.Thread(target=_run, daemon=True).start()
