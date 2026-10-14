"""Queue Krea2 txt2img calibration runs on a local ComfyUI and wait for them.

Usage (plan is a JSON list; "sda": 1 gates the SDA LoRA to steps 0-2 of 8):

    python scripts/krea2_calibrate.py '[{"seed": 101, "sampler": "dpmpp_2m_sde_gpu",
        "scheduler": "sgm_uniform", "sda": 1, "prefix": "MediaStudio/Calibration/krea2_C_seed101"}]'

Results land in the ComfyUI output folder under the given prefix. See
docs/CREATE-KREA2-PLAN.md (section 5) for the 2026-10-10 calibration.
"""
import json
import sys
import time
import urllib.request
import uuid

HOST = "http://127.0.0.1:8188"

PROMPT = (
    "A photorealistic indoor portrait captured at eye level from a medium-close distance, "
    "with soft overcast daylight coming from a large window on the left.\n\n"
    "In the center of the frame, a young woman in her mid-twenties with a slim build sits at a "
    "small wooden cafe table, her body turned slightly toward the window while her head faces the "
    "camera. She has fair skin with visible natural pores and a few faint freckles across her nose, "
    "shoulder-length wavy auburn hair tucked behind her right ear, light hazel-green eyes, and full "
    "lips with a soft rose lipstick. She wears a loose cream knitted sweater with a wide neckline that "
    "reveals her collarbones. Her right hand holds a white ceramic cup near her chin while her left "
    "forearm rests flat on the table beside an open notebook.\n\n"
    "Behind her, the cafe interior shows exposed brick walls, hanging plants, and a blurred counter "
    "with warm pendant lamps.\n\n"
    "The woman's face and hands remain sharply focused while the background softens naturally, with "
    "balanced framing and clear separation between the subject and the interior."
)


def graph(prompt, seed, sampler, scheduler, width, height, prefix):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "moodyKrea2Mix_v40.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": sampler, "scheduler": scheduler,
            "denoise": 1.0, "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["8", 0]}},
    }


def graph_sda(prompt, seed, sampler, scheduler, width, height, prefix, gate=2):
    g = graph(prompt, seed, sampler, scheduler, width, height, prefix)
    del g["7"]
    g["10"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
        "model": ["1", 0], "lora_name": "krea2_turbo_sda_v1.0_comfy.safetensors", "strength_model": 1.0}}
    common = {"noise_seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": sampler, "scheduler": scheduler,
              "positive": ["4", 0], "negative": ["5", 0]}
    g["11"] = {"class_type": "KSamplerAdvanced", "inputs": {**common, "model": ["10", 0], "latent_image": ["6", 0],
               "add_noise": "enable", "start_at_step": 0, "end_at_step": gate, "return_with_leftover_noise": "enable"}}
    g["12"] = {"class_type": "KSamplerAdvanced", "inputs": {**common, "model": ["1", 0], "latent_image": ["11", 0],
               "add_noise": "disable", "start_at_step": gate, "end_at_step": 10000, "return_with_leftover_noise": "disable"}}
    g["8"]["inputs"]["samples"] = ["12", 0]
    return g


def post(path, body):
    req = urllib.request.Request(HOST + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get(path):
    with urllib.request.urlopen(HOST + path, timeout=30) as r:
        return json.load(r)


def main():
    plan = json.loads(sys.argv[1])
    client_id = str(uuid.uuid4())
    queued = []
    for item in plan:
        build = graph_sda if item.get("sda") else graph
        g = build(PROMPT, item["seed"], item["sampler"], item["scheduler"], item.get("w", 896), item.get("h", 1152), item["prefix"])
        res = post("/prompt", {"prompt": g, "client_id": client_id})
        queued.append((item, res["prompt_id"], time.monotonic()))
        print("queued", item["prefix"], res["prompt_id"], flush=True)
    pending = {pid: (item, t0) for item, pid, t0 in queued}
    last_done = time.monotonic()
    while pending:
        time.sleep(3)
        for pid in list(pending):
            hist = get(f"/history/{pid}")
            if pid not in hist:
                continue
            item, _ = pending.pop(pid)
            entry = hist[pid]
            status = entry.get("status", {})
            files = [img["filename"] for out in entry.get("outputs", {}).values() for img in out.get("images", [])]
            now = time.monotonic()
            msgs = [m[0] for m in status.get("messages", [])]
            print(json.dumps({"prefix": item["prefix"], "status": status.get("status_str"),
                              "seconds_since_prev": round(now - last_done, 1), "files": files,
                              "errors": [m for m in status.get("messages", []) if m[0] == "execution_error"][:1] or None,
                              "events": msgs}), flush=True)
            last_done = now


if __name__ == "__main__":
    main()
