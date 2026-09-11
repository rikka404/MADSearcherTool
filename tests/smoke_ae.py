"""Prepare an isolated, real After Effects JSX import/render smoke fixture.

This never saves or closes a project. Run the printed verification JSX with
AfterFX.exe -r, then run this script with --verify <fixture_directory>.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
from mad_worker.exporters import export_ae


def prepare(directory):
    directory.mkdir(parents=True, exist_ok=False)
    sources, masks = directory / "source_frames", directory / "masks"
    sources.mkdir()
    masks.mkdir()
    alpha = np.zeros((128, 128), dtype=np.uint8)
    alpha[8:120, 8:120] = 255
    alpha[28:100, 28:100] = 0
    alpha[48:80, 48:80] = 255
    last = np.zeros_like(alpha)
    last[15:45, 15:45] = 255
    last[85:110, 85:115] = 255
    for index, plane in enumerate((alpha, np.zeros_like(alpha), last)):
        Image.new("RGB", (128, 128), (215, 62, 84)).save(sources / f"{index:06d}.png")
        Image.fromarray(plane).save(masks / f"{index:06d}.png")
    jsx = export_ae(sources, sorted(masks.glob("*.png")), 8., 128, 128, directory / "import_cutout.jsx")
    target_name = "MADSearcher_SMOKE_" + directory.name
    report = directory / "ae_result.txt"
    # Redirect alert locally to a throwing function: errors cannot open a modal.
    template = r'''#target aftereffects
(function () {
    var output = new File(__REPORT__);
    output.encoding = "UTF-8";
    var ownedName = __NAME__;
    var previous = app.project ? app.project.activeItem : null;
    try {
        if (!app.project) app.newProject();
        for (var i = 1; i <= app.project.numItems; i++) {
            if (app.project.item(i).name === ownedName) throw new Error("Fixture already exists; refusing duplicate execution.");
        }
        var comp = app.project.items.addComp(ownedName, 128, 128, 1, 1, 8);
        comp.openInViewer();
        comp.time = 0;
        var source = new File(__SCRIPT__);
        source.encoding = "UTF-8";
        if (!source.open("r")) throw new Error("Cannot read generated JSX");
        var script = source.read(); source.close();
        script = script.replace(/^#target[^\r\n]*/, "").replace(/\balert\s*\(/g, "fixtureAlert(");
        function fixtureAlert(message) { throw new Error(message); }
        eval(script);
        if (comp.numLayers !== 1) throw new Error("Expected exactly one imported video layer");
        var layer = comp.layer(1);
        var group = layer.property("ADBE Mask Parade");
        if (group.numProperties !== 4) throw new Error("Expected four depth-preserving mask slots");
        var keyCounts = [];
        for (var m = 1; m <= group.numProperties; m++) {
            var path = group.property(m).property("ADBE Mask Shape");
            if (path.numKeys !== 3) throw new Error("Expected three shape keyframes");
            for (var key = 1; key <= path.numKeys; key++) {
                if (path.keyOutInterpolationType(key) !== KeyframeInterpolationType.HOLD) throw new Error("Mask keyframes are not HOLD");
            }
            keyCounts.push(path.numKeys);
        }
        for (var frame = 0; frame < 3; frame++) comp.saveFrameToPng(frame / 8, new File(__DIRECTORY__ + "/render_" + frame + ".png"));
        if (!output.open("w")) throw new Error("Cannot write verification report");
        output.writeln("status=passed");
        output.writeln("ae_version=" + app.version);
        output.writeln("comp=" + comp.name);
        output.writeln("layers=" + comp.numLayers);
        output.writeln("masks=" + group.numProperties);
        output.writeln("key_counts=" + keyCounts.join(","));
        output.writeln("interpolation=HOLD");
        output.writeln("project_saved=false");
        output.close();
    } catch (error) {
        if (output.open("w")) { output.writeln("status=failed"); output.writeln("error=" + error.toString()); output.writeln("line=" + error.line); output.close(); }
    } finally {
        if (previous instanceof CompItem) previous.openInViewer();
    }
})();
'''
    replacements = {"__REPORT__": str(report), "__NAME__": target_name, "__SCRIPT__": jsx, "__DIRECTORY__": str(directory)}
    for token, value in replacements.items():
        template = template.replace(token, json.dumps(value.replace("\\", "/"), ensure_ascii=True))
    verification = directory / "verify_in_ae.jsx"
    verification.write_text(template, encoding="utf-8")
    print(str(verification))


def verify(directory):
    content = (directory / "ae_result.txt").read_text(encoding="utf-8-sig")
    assert "status=passed" in content, content
    alpha_planes = []
    for index in range(3):
        with Image.open(directory / f"render_{index}.png") as image:
            assert "A" in image.getbands(), "AE render did not preserve an alpha plane"
            alpha_planes.append(np.array(image.getchannel("A")))
    first, empty, last = alpha_planes
    assert first[15, 15] > 250, "Outer ADD contour disappeared"
    assert first[38, 38] < 5, "SUBTRACT hole filled incorrectly"
    assert first[60, 60] > 250, "Island inside the hole disappeared"
    assert int(empty.max()) == 0, "Empty frame exposes original RGB footage"
    assert last[25, 25] > 250 and last[95, 95] > 250 and last[60, 60] < 5
    report = {"status": "passed", "ae_execution_verified": True, "actual_png_alpha_verified": True,
              "hole_verified": True, "island_verified": True, "empty_frame_verified": True,
              "disconnected_components_verified": True, "project_saved": False, "ae_report": content}
    (directory / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "artifacts" / "ae-smoke" / datetime.now().strftime("%Y%m%d_%H%M%S")))
    parser.add_argument("--verify")
    arguments = parser.parse_args()
    if arguments.verify:
        verify(Path(arguments.verify).resolve())
    else:
        prepare(Path(arguments.output_dir).resolve())
