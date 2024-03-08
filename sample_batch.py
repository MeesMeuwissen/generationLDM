from aiosynawsmodules.services.sso import set_sso_profile
import logging

from aiosynawsmodules.services.batch import submit_batch_job

set_sso_profile(profile_name="aws-aiosyn-workloads-dev", region_name="eu-west-1")  #What is the correct profile?
logging.basicConfig(level=logging.INFO)

submit_batch_job(
    name="generation_sample",
    script_path= "generationLDM/sampling/generate_synthetic_dataset.py", #The script to be executed
    timeout_min=60 * 5, #Time in minutes
    gpu=True,
    account="computing-feature2", #For me, this is 2
    gpus=0,
    location='remote',
    n_attempts=1,
)