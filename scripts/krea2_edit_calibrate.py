"""Calibrate the Krea2 Identity Edit graph of the krea2_moody_v40 profile live.

The graph is compiled by the profile's own compile_edit_workflow, then the
calibrated knobs are overridden per run. Usage (plan is a JSON list):

    python scripts/krea2_edit_calibrate.py SOURCE.png "edit command" '[{"label": "A",
        "sampler": "euler_ancestral", "scheduler": "sgm_uniform", "ref_boost": 4,
        "grounding_px": 768, "aspect": "source"}]'

The plan may also be a file: '@plan.json'. Add --front to jump the ComfyUI queue. Optional per-run keys: instruction (overrides the shared command), seed
(default 20261011), aspect (a profile ratio such as "3:4" or "source"),
edit_profile (a manifest edit profile id; the knobs below override it), unet (another
Krea2 diffusion model file instead of the profile's),
cfg, steps, ref_boost_a, system_prompt, negative (what must disappear; acts at cfg > 1), lora / lora_strength (replace the identity edit
LoRA), extra_lora / extra_lora_strength (chained after it), subject_image (a donor photo
wired by the profile as the second reference: scene first, subject second). Results land in ComfyUI
output under CMV/CalibrationEdit/. See docs/CREATE-KREA2-PLAN.md (sections 5.1, 5.2).
"""
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image

from app.comfyui.client import ComfyUIClient
from app.comfyui.simple_profiles import (
    compile_edit_workflow,
    load_approved_profiles,
    resolve_dimensions,
)

DEFAULT_SEED = 20261011


def node_of(workflow, class_type):
    return [node for node in workflow.values() if node["class_type"] == class_type]


def node_id(workflow, class_type):
    return next(key for key, node in workflow.items() if node["class_type"] == class_type)


def upload(client, path: Path) -> str:
    uploaded = client.upload_image(f"cmv_calib_{path.stem}{path.suffix}", path.read_bytes(), overwrite=True)
    return "/".join(part for part in (uploaded.get("subfolder"), uploaded["name"]) if part)


def main():
    source_path = Path(sys.argv[1])
    shared_instruction = sys.argv[2]
    raw_plan = sys.argv[3]
    plan = json.loads(Path(raw_plan[1:]).read_text(encoding="utf-8") if raw_plan.startswith("@") else raw_plan)

    profile = load_approved_profiles()["krea2_moody_v40"]
    client = ComfyUIClient(timeout=30)
    source_name = upload(client, source_path)
    with Image.open(source_path) as image:
        source_size = image.size

    queued = []
    for run in plan:
        dimensions = resolve_dimensions(profile, run.get("aspect", "source"), source_size)
        subject_name = upload(client, Path(run["subject_image"])) if run.get("subject_image") else None
        workflow = compile_edit_workflow(
            profile,
            instruction=run.get("instruction", shared_instruction),
            source_image=source_name,
            dimensions=dimensions,
            seed=int(run.get("seed", DEFAULT_SEED)),
            edit_profile=run.get("edit_profile"),
            subject_image=subject_name,
            negative_instruction=run.get("negative", ""),
        )
        sampler = node_of(workflow, "KSampler")[0]["inputs"]
        sampler["sampler_name"] = run.get("sampler", sampler["sampler_name"])
        sampler["scheduler"] = run.get("scheduler", sampler["scheduler"])
        sampler["cfg"] = float(run.get("cfg", sampler["cfg"]))
        sampler["steps"] = int(run.get("steps", sampler["steps"]))
        patch = node_of(workflow, "Krea2EditModelPatch")[0]["inputs"]
        patch["ref_boost"] = float(run.get("ref_boost", patch["ref_boost"]))
        patch["ref_boost_a"] = float(run.get("ref_boost_a", patch["ref_boost_a"]))
        for encode in node_of(workflow, "Krea2EditGroundedEncode"):
            encode["inputs"]["grounding_px"] = int(run.get("grounding_px", encode["inputs"]["grounding_px"]))
            if "system_prompt" in run:
                encode["inputs"]["system_prompt"] = run["system_prompt"]
        if run.get("unet"):
            node_of(workflow, "UNETLoader")[0]["inputs"]["unet_name"] = run["unet"]
        lora = node_of(workflow, "LoraLoaderModelOnly")[0]["inputs"]
        lora["lora_name"] = run.get("lora", lora["lora_name"])
        lora["strength_model"] = float(run.get("lora_strength", lora["strength_model"]))
        if run.get("extra_lora"):
            lora_id = node_id(workflow, "LoraLoaderModelOnly")
            workflow["33"] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {
                    "model": [lora_id, 0],
                    "lora_name": run["extra_lora"],
                    "strength_model": float(run.get("extra_lora_strength", 1.0)),
                },
            }
            patch["model"] = ["33", 0]
        node_of(workflow, "SaveImage")[0]["inputs"]["filename_prefix"] = f"CMV/CalibrationEdit/edit_{run['label']}"
        if "--front" in sys.argv:
            payload = {"prompt": workflow, "client_id": str(uuid.uuid4()), "front": True}
            prompt_id = client._request_json("POST", "/prompt", payload=payload, timeout=30.0)["prompt_id"]
        else:
            prompt_id = client.queue_prompt(workflow, client_id=str(uuid.uuid4()))["prompt_id"]
        queued.append((run["label"], prompt_id, dimensions))
        print(f"queued {run['label']} {dimensions} -> {prompt_id}", flush=True)

    previous_done = time.monotonic()
    for label, prompt_id, dimensions in queued:
        while True:
            history = client.get_history(prompt_id)
            entry = history.get(prompt_id)
            if entry and entry.get("status", {}).get("completed") is not None:
                break
            time.sleep(2)
        status = entry["status"]
        files = [
            image["filename"]
            for output in entry.get("outputs", {}).values()
            for image in output.get("images", [])
        ]
        now = time.monotonic()
        print(
            f"{label}: {status.get('status_str')} {dimensions} {now - previous_done:.1f}s {files}",
            flush=True,
        )
        if status.get("status_str") != "success":
            print(json.dumps(status.get("messages", [])[-2:], ensure_ascii=False)[:2000])
        previous_done = now


if __name__ == "__main__":
    main()
