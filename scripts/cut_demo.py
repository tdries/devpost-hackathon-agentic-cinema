"""Cut each recorded beat to its narration, lay the overlays and the track on.

    python3 scripts/cut_demo.py [outname]   ->  docs/video/<outname>.mp4

Each beat is trimmed to the length of docs/voiceover/NN.wav plus a held moment.
A chip naming the screen fades in at the top of every beat; callouts point at
whatever the narration is talking about at that moment.
"""
import glob, os, subprocess, sys

OUT = "docs/video"
NAME = sys.argv[1] if len(sys.argv) > 1 else "demo"
GAP = 0.2
NBEATS = 15
SKIP = {3: 4.0, 14: 30.0}      # a head the beat needed but the film does not

CHIP_IN, CHIP_HOLD = 0.5, 4.0


def dur(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    if out in ("", "N/A"):     # webm from the recorder can lack a duration header
        out = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams",
                              "v:0", "-show_entries", "stream=nb_read_packets,avg_frame_rate",
                              "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
        packets, rate = out.split(",")[0], out.split(",")[1]
        num, den = (rate.split("/") + ["1"])[:2]
        return int(packets) / (float(num) / float(den or 1))
    return float(out)


def layers(n, length):
    """Every still this beat lays over the footage, with when it comes and goes."""
    out = []
    if n == 1:
        # one tag per spoken line, on the shot that line is about
        import json
        offs = json.load(open("docs/voiceover/marks.json"))["1"] + [length]
        for i in range(len(offs) - 1):
            f = f"{OUT}/ov/tag-01-{i}.png"
            a = max(offs[i] + 0.5, 4.8 if i == 0 else 0)
            b = min(offs[i + 1] - 0.15, length - 0.2)
            if os.path.exists(f) and b - a > 0.8:
                out.append((f, a, b))
        return out
    chip = f"{OUT}/ov/chip-{n:02d}.png"
    if os.path.exists(chip) and length > CHIP_IN + 2:
        out.append((chip, CHIP_IN, min(CHIP_IN + CHIP_HOLD, length - 0.3)))
    # callouts are drawn where the recorder actually found the element, at the
    # moment it was there: a guessed rectangle points at the wrong thing.
    spots = f"{OUT}/beats/{n:02d}/spots.json"
    if os.path.exists(spots):
        import json
        sys.path.insert(0, "scripts")
        from make_overlays import callout
        for i, sp in enumerate(json.load(open(spots))):
            a = sp["t"] - SKIP.get(n, 0.0)
            b = min(a + sp["hold"], length - 0.25)
            if a < 0.15 or b - a < 1.0:
                continue
            f = f"{OUT}/ov/spot-{n:02d}-{i}.png"
            x, y, w, h = sp["box"]
            callout(x, y, w, h, sp["label"]).save(f)
            out.append((f, a, b))
    return out


os.makedirs(f"{OUT}/cut", exist_ok=True)
parts = []
for n in range(1, NBEATS + 1):
    src = sorted(glob.glob(f"{OUT}/beats/{n:02d}/*.webm") + glob.glob(f"{OUT}/beats/{n:02d}/*.mp4"))[0]
    want = dur(f"docs/voiceover/{n:02d}.wav") + GAP
    dst = f"{OUT}/cut/{n:02d}.mp4"
    over = layers(n, want)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{SKIP.get(n, 0):.1f}", "-i", src]
    for f, _, _ in over:
        cmd += ["-loop", "1", "-t", f"{want:.2f}", "-i", f]
    chain = ("[0:v]fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
             "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[v0];")
    for i, (_, a, b) in enumerate(over, 1):
        # in over a third of a second, out over a half: quick enough to feel
        # deliberate, slow enough not to flicker
        chain += (f"[{i}:v]format=rgba,fade=t=in:st={a:.2f}:d=0.33:alpha=1,"
                  f"fade=t=out:st={max(a, b - 0.5):.2f}:d=0.5:alpha=1[o{i}];")
        chain += (f"[v{i - 1}][o{i}]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[v{i}];")
    chain = chain.rstrip(";")
    cmd += (["-filter_complex", chain, "-map", f"[v{len(over)}]"] if over
            else ["-vf", "fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
                         "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"])
    cmd += ["-t", f"{want:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", dst]
    subprocess.run(cmd, check=True)
    print(f"{n:02d}  {dur(dst):5.1f}s  {len(over)} overlay(s)")
    parts.append(dst)

# the mark returns over the last beat, fading up as the narration ends
last, tail = parts[-1], f"{OUT}/cut/{NBEATS:02d}e.mp4"
hold = max(0.0, dur(last) - 4.6)
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", last,
                "-loop", "1", "-t", f"{dur(last):.2f}", "-i", f"{OUT}/endcard.png",
                "-filter_complex",
                f"[1:v]format=rgba,fade=t=in:st={hold:.2f}:d=1.0:alpha=1[t];"
                f"[0:v][t]overlay=0:0:enable='gte(t,{hold:.2f})'",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", tail], check=True)
parts[-1] = tail

with open(f"{OUT}/cut/list.txt", "w") as f:
    for p in parts:
        f.write(f"file '{os.path.basename(p)}'\n")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", f"{OUT}/cut/list.txt", "-c", "copy", f"{OUT}/silent.mp4"], check=True)
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f"{OUT}/silent.mp4",
                "-i", "docs/voiceover/demo-voiceover.wav", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest", f"{OUT}/{NAME}.mp4"], check=True)
print(f"{NAME}.mp4  video {dur(f'{OUT}/silent.mp4'):.1f}s  audio {dur('docs/voiceover/demo-voiceover.wav'):.1f}s")
