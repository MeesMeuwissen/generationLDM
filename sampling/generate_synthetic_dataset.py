import argparse
import os
import sys
import uuid
from datetime import datetime
import numpy as np
from pathlib import Path
import zipfile
import glob

import torch
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.util import instantiate_from_config
from omegaconf import OmegaConf
from torchvision import transforms
from tqdm import tqdm
from pytorch_fid.fid_score import calculate_activation_statistics, calculate_frechet_distance
from pytorch_fid.inception import InceptionV3

from aiosynawsmodules.services.s3 import upload_file, download_file
from aiosynawsmodules.services.sso import set_sso_profile


def load_model_from_config(config, ckpt, device):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    model.to(device)
    model.eval()
    return model


def get_model(config_path, device, checkpoint):
    config = OmegaConf.load(config_path)
    model = load_model_from_config(config, checkpoint, device)
    return model


def get_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-l",
        "--location",
        type=str,
        const=True,
        default="maclocal",
        nargs="?",
        help="Running local, maclocal or remote",
    )
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        const=True,
        default=False,
        nargs="?",
        help="Model to use for sampling"
    )
    parser.add_argument(
        "-s",
        "--summary",
        type=str,
        const=True,
        default="A H&E stained slide of a piece of kidney tissue",
        nargs="?",
        help="Summary input for conditioning"
    )
    parser.add_argument(
        "-t",
        "--tumor_desc",
        type=str,
        const=True,
        default="High tumor; low TIL;",
        nargs="?",
        help="Tumor conditioning description"
    )
    parser.add_argument(
        "-n",
        "--number",
        type=int,
        const=True,
        default=1500,
        nargs="?",
        help="Nr of samples to generate"
    )
    return parser


def get_samples(model, shape, batch_size, depth_of_sampling, summary, tumor_desc):
    scale = 1.5  # Scale of classifier free guidance

    sampler = DDIMSampler(model)

    def get_unconditional_token(batch_size):
        return [""] * batch_size

    def get_conditional_token(batch_size, summary):
        # append tumor and TIL probability to the summary
        tumor = [tumor_desc] * (batch_size)  # Keep this
        return [t + summary for t in tumor]

    with torch.no_grad():
        # unconditional token for classifier free guidance
        ut = get_unconditional_token(batch_size)
        uc = model.get_learned_conditioning(ut).to(torch.float32)

        ct = get_conditional_token(batch_size, summary)
        cc = model.get_learned_conditioning(ct).to(torch.float32)

        samples_ddim, _ = sampler.sample(
            50,
            batch_size,
            shape,
            cc,
            verbose=False,
            unconditional_guidance_scale=scale,
            unconditional_conditioning=uc,
            eta=0,
            use_tqdm=False,
        )
        x_samples_ddim = model.decode_first_stage(samples_ddim)
        x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
        x_samples_ddim = (x_samples_ddim * 255).to(torch.uint8).cpu()

        return x_samples_ddim


def save_sample(sample, output_dir):
    image_uuid = str(uuid.uuid4())
    image_path = os.path.join(output_dir, image_uuid + ".png")
    to_pil = transforms.ToPILImage()

    sample = to_pil(sample)
    sample.save(image_path)
    return image_path


def main(model_path, size, summary, tumor_desc, nr_of_samples=1500):
    device = torch.device(
        "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    )
    config_path = "generationLDM/configs/sampling/sampling.yaml"
    save_to_s3 = True

    # Model will be downloaded to /home/aiosyn/model.ckpt (see download_model())
    print("Downloading model ...")
    download_model(model_path)

    depth_of_sampling = 50  # Steps in the sampling process
    batch_size = 8  # 256 with batch size 4 crashes aws (out of memory)
    shape = [3, size, size]

    now = datetime.now()
    formatted_now = now.strftime("%m-%d_%H%M")
    output_dir = f"/home/aiosyn/data/generated_samples/{formatted_now}_size={4*size}"
    os.makedirs(output_dir, exist_ok=True)
    model = get_model("code/" + config_path, device, "/home/aiosyn/model.ckpt")

    print(f"Generating {nr_of_samples} synthetic images of size {4*size} ...")
    print(f"Saving to {output_dir} ... ")
    img_paths = []
    for i in tqdm(range(nr_of_samples // batch_size + 1)):
        samples = get_samples(model, shape, batch_size, depth_of_sampling, summary, tumor_desc)
        for sample in samples:
            path = save_sample(sample, output_dir)
            img_paths.append(path)

    fid = calculate_FID(paths=img_paths, device=device)
    # save some metadata to a file in output dir as well.

    with open(output_dir + "/metadata.txt", "w") as f:
        f.write(f"Model path used: {model_path}\n")
        f.write(f"FID compared to real data: {fid}\n")
        f.write(f"Depth of sampling: {depth_of_sampling}\n")
        f.write(f"Number of samples: {nr_of_samples}\n")
        f.write(f"Batch size: {batch_size}\n")
        f.write(f"Summary used: {summary}\n")
        f.write(f"Tumor description: {tumor_desc}\n")

    if save_to_s3 or opt.location == "remote":
        print(f"Saving samples in {output_dir} to S3 ...")
        # set_sso_profile("aws-aiosyn-data", region_name="eu-west-1")

        zip_directory(output_dir, "generated_images.zip")
        upload_file(
            "generated_images.zip",
            f"s3://aiosyn-data-eu-west-1-bucket-ops/patch_datasets/generation/synthetic-data/{formatted_now}-size={4*size}/",
        )
        upload_file(
            output_dir + "/metadata.txt",
            f"s3://aiosyn-data-eu-west-1-bucket-ops/patch_datasets/generation/synthetic-data/{formatted_now}-size={4*size}/metadata.txt"
        )
    print("Done")

def zip_directory(directory, zip_filename):
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file in glob.glob(os.path.join(directory, '*.png')):
            zipf.write(file, os.path.relpath(file, directory))


def calculate_FID(paths, device):
    model = InceptionV3().to(device)
    mu_fake, sig_fake = calculate_activation_statistics(paths, model, device=device)
    with np.load(Path("/home/aiosyn/code/generationLDM/FID/FID_outputs/FID_full.npz")) as f:
        m1, s1 = f["mu"], f["sig"]

    fid = calculate_frechet_distance(m1, s1, mu_fake, sig_fake)
    print(f"Calculated FID: {fid}")
    return fid

def add_taming_lib(loc):
    if loc in ["local", "maclocal"]:
        taming_dir = os.path.abspath("generationLDM/src/taming-transformers")
    elif loc == "remote":
        taming_dir = os.path.abspath("code/generationLDM/src/taming-transformers")
    else:
        assert False, "Unknown location"
    sys.path.append(taming_dir)


def download_model(path):
    # Running remotely, so model needs to be downloaded
    download_file(
        remote_path=path,
        local_path="/home/aiosyn/model.ckpt",
    )
    sys.path.append("/home/aiosyn/code")


if __name__ == "__main__":
    parser = get_parser()

    opt, unknown = parser.parse_known_args()
    add_taming_lib(opt.location)
    size = 64  # Remember that the autoencoder upscales them by 4x!
    summary = opt.summary
    tumor_desc = opt.tumor_desc
    nr_of_samples = opt.number
    model_path = opt.model

    main(model_path, size=size, summary=summary, tumor_desc=tumor_desc, nr_of_samples=nr_of_samples)
