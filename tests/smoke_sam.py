"""Explicit real-model smoke test. Not part of the fast unittest suite.

Usage: .venv/Scripts/python.exe tests/smoke_sam.py --checkpoint models/sam2.1_hiera_tiny.pt
It writes only synthetic media and results beneath artifacts/sam-smoke by default.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from mad_worker import cutout
from mad_worker.context import Context


def main():
    parser = argparse.ArgumentParser(description="Run real SAM 2 tracking on a synthetic moving character")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--output-dir", default=str(ROOT / "artifacts" / "sam-smoke" / datetime.now().strftime("%Y%m%d_%H%M%S")))
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_frames = output / "synthetic_frames"
    source_frames.mkdir()
    true_masks = []
    for index in range(8):
        image = Image.new("RGB", (160, 120), (55, 69, 94))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 99, 159, 119), fill=(70, 87, 103))
        mask = Image.new("L", image.size, 0)
        alpha_draw = ImageDraw.Draw(mask)
        offset = index * 2
        shapes = [("ellipse", (45 + offset, 18, 79 + offset, 54), (236, 177, 133)),
                  ("polygon", [(48 + offset, 50), (76 + offset, 50), (84 + offset, 92), (40 + offset, 92)], (210, 43, 67)),
                  ("rectangle", (46 + offset, 91, 56 + offset, 108), (30, 27, 47)),
                  ("rectangle", (69 + offset, 91, 79 + offset, 108), (30, 27, 47))]
        for kind, coordinates, colour in shapes:
            getattr(draw, kind)(coordinates, fill=colour)
            getattr(alpha_draw, kind)(coordinates, fill=255)
        draw.ellipse((54 + offset, 32, 57 + offset, 36), fill=(33, 29, 41))
        draw.ellipse((67 + offset, 32, 70 + offset, 36), fill=(33, 29, 41))
        image.save(source_frames / f"{index:06d}.png")
        true_masks.append(np.asarray(mask) >= 128)
        if index == 0:
            mask.save(output / "first_mask.png")
    events = []
    def emit(event):
        events.append(event)
        print(json.dumps(event, ensure_ascii=False), flush=True)
    ctx = Context(str(output / "workspace"), {"sam_checkpoint": str(Path(args.checkpoint).resolve()),
                  "sam_config": "configs/sam2.1/sam2.1_hiera_t.yaml", "device": args.device}, emit)
    source = output / "synthetic.mp4"
    ctx.media.run(["-framerate", "8", "-i", source_frames / "%06d.png", "-c:v", "libx264",
                   "-crf", "0", "-pix_fmt", "yuv420p", source])
    # Three real inference chunks (0..3, 3..6, 6..7) exercise overlap handoff.
    with patch.object(cutout, "BLOCK_FRAMES", 4):
        result = cutout.handle("cutout.run", {"path": str(source), "start": 0, "end": 1,
                 "prompt_mode": "mask", "mask_path": str(output / "first_mask.png"),
                 "output_dir": str(output / "results"), "export_video": True, "export_ae": True}, ctx)
    assert result["frame_count"] == 8
    assert result["ae_script"] and Path(result["ae_script"]).is_file()
    ious = []
    for index, path in enumerate(sorted(Path(result["rgba_dir"]).glob("*.png"))):
        with Image.open(path) as image:
            assert image.mode == "RGBA"
            alpha = np.asarray(image)[:, :, 3]
        prediction = alpha >= 128
        assert alpha.min() == 0 and alpha.max() == 255, f"Frame {index} lacks foreground/background"
        ious.append(float(np.logical_and(prediction, true_masks[index]).sum() / np.logical_or(prediction, true_masks[index]).sum()))
    assert min(ious) > .3, f"Synthetic target tracking too weak: {ious}"
    assert ious[0] == 1., "The supplied first-frame mask was not preserved"
    assert ctx.media.probe(result["video_path"])["codec"] == "prores"
    decoded = output / "mov_alpha.png"
    ctx.media.run(["-i", result["video_path"], "-vf", "alphaextract", "-frames:v", "1", "-update", "1", decoded])
    with Image.open(decoded) as image:
        recovered = np.asarray(image.convert("L"))
    assert recovered.min() <= 1 and recovered.max() >= 250
    report = {"status": "passed", "device": args.device, "frame_count": 8, "block_frames": 4,
              "ious": ious, "mean_iou": sum(ious) / len(ious), "mov_alpha_verified": True,
              "ae_script_generated": True, "ae_execution_verified": False, "result": result}
    (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
