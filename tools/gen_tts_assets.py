"""Regenerate the bundled fixed-alert TTS clips.

Why these are BUNDLED (not runtime-generated):
    tts_helper.py speaks via edge-tts (Microsoft neural voices over the net),
    falling back to robotic Windows SAPI5 when that fails. Microsoft rotates the
    Edge read-aloud handshake at will — when they did, edge-tts 6.x started
    returning 403s and every alert cue silently degraded to the robotic SAPI
    voice. The four fixed fleet/decloak cues are safety-critical, so they must
    NOT depend on a live network call at alert time. We pre-render them once,
    commit the .mp3s, and tts_helper plays the bundled file directly (see
    BUNDLED_CLIPS in tts_helper.py). They are shipped like fire_alert.mp3.

Voice:
    en-US-ChristopherNeural — a deep male voice the owner auditioned and picked
    for all fixed alert cues. Keep this in sync with EDGE_VOICE in tts_helper.py
    (the dynamic, non-bundled speech path uses the same voice so everything
    sounds consistent).

How to re-run (requires a WORKING edge-tts, i.e. >=7.0.0; 6.x is dead against
Microsoft's current handshake):
    py -3.12 tools/gen_tts_assets.py

Overwrites assets/tts/*.mp3 in place. Commit the results — the repo is the
distribution channel and these are assets, not build output.
"""

import asyncio
import os
import sys

import edge_tts  # type: ignore

VOICE = "en-US-ChristopherNeural"

# out_filename -> spoken text.
# NOTE: the spoken text for the decloak cue is "Decloaked!" (exclamation for
# punch), but the LOOKUP KEY in tts_helper.BUNDLED_CLIPS / fc_gui stays exactly
# "Decloaked". The other three spoken strings match their lookup keys verbatim.
CLIPS = {
    "fleet_lost_10.mp3": "Ten percent of fleet lost",
    "fleet_lost_25.mp3": "Twenty five percent of fleet lost",
    "fleet_lost_50.mp3": "Fifty percent of fleet lost",
    "decloaked.mp3": "Decloaked!",
}

# assets/tts/ next to the repo root (this file lives in tools/).
OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "tts",
)


async def _gen_one(text: str, out_path: str) -> None:
    comm = edge_tts.Communicate(text, VOICE)
    await comm.save(out_path)


async def _main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    ok = True
    for fname, text in CLIPS.items():
        out_path = os.path.join(OUT_DIR, fname)
        print(f"[gen] {VOICE!r}: {text!r} -> {out_path}")
        await _gen_one(text, out_path)
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if size <= 5 * 1024:
            print(f"  !! FAILED: {fname} is only {size} bytes (expected >5KB)")
            ok = False
        else:
            print(f"  ok: {size} bytes")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
