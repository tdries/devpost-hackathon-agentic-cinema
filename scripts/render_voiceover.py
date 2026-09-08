"""Render the demo voiceover with Kokoro: one WAV per beat, plus the full track.

Usage: ~/.jarvis/venv/bin/python scripts/render_voiceover.py [voice] [speed]  (speed multiplies the per-beat PACE)
Model files default to ~/.jarvis/models (KOKORO_MODEL / KOKORO_VOICES override). Needs `brew install espeak-ng` on macOS. Reads the Voiceover column of docs/demo-video-script.md. ponytail: markdown table
parsing by splitting on pipes; the table has no pipes inside cells.
"""
import os, re, sys
os.environ.setdefault("ESPEAK_DATA_PATH", "/opt/homebrew/share/espeak-ng-data")
os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY", "/opt/homebrew/lib/libespeak-ng.dylib")
import numpy as np, soundfile as sf
from kokoro_onnx import Kokoro

HOME = os.path.expanduser("~")
model = os.environ.get("KOKORO_MODEL", f"{HOME}/.jarvis/models/kokoro-v1.0.onnx")
voices = os.environ.get("KOKORO_VOICES", f"{HOME}/.jarvis/models/voices-v1.0.bin")
voice, speed = (sys.argv[1:2] or ["af_heart"])[0], float((sys.argv[2:3] or ["1.0"])[0])
# ponytail: slower where the point lands (evidence, the guard, the close), quicker on the explaining
PACE = {1: 1.0, 2: 1.02, 3: 1.04, 4: 1.06, 5: 1.04, 6: 1.06, 7: 1.02, 8: 1.04, 9: 1.02, 10: 1.02, 11: 1.02, 12: 1.02, 13: 1.06, 14: 1.04, 15: 1.0}
rows = [l for l in open("docs/demo-video-script.md") if re.match(r"\| \d+ \|", l)]
beats = [l.split("|")[5].strip() for l in rows]

# ponytail: spelling fixes for the English voice, applied to the audio only
say = {"Loi Evin": "Loo-ah Evan", "Legifrance": "Lejee-france", "The Media Customs": "The Media Customs",
       "MCP": "M C P", "Veo": "Vay-oh", "Mimir": "Mee-meer"}
kk = Kokoro(model, voices)
gap = np.zeros(int(0.35 * 24000), dtype=np.float32)
full, total = [], 0.0
for i, text in enumerate(beats, 1):
    for k, v in say.items():
        text = text.replace(k, v)
    audio, sr = kk.create(text, voice=voice, speed=speed * PACE.get(i, 1.0), lang="en-us")
    audio = audio.astype(np.float32)
    sf.write(f"docs/voiceover/{i:02d}.wav", audio, sr)
    d = len(audio) / sr
    total += d
    print(f"{i:02d}  {d:5.1f}s  {text[:60]}")
    full += [audio, gap]
sf.write("docs/voiceover/demo-voiceover.wav", np.concatenate(full[:-1]), sr)
print(f"total speech {total:.1f}s, with gaps {total + 0.35 * (len(beats) - 1):.1f}s")
