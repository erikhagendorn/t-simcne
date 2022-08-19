import os
import re
import subprocess
import time
from pathlib import Path


def redo(deplist, exe="redo"):
    if isinstance(deplist, (str, Path)):
        deplist = [deplist]
    return subprocess.call([exe] + deplist, close_fds=False)


def redo_ifchange(deplist):
    return redo(deplist, "redo-ifchange")


def redo_ifchange_slurm(
    deplist,
    partition="gpu-v100",
    n_cpus=None,
    mem=None,
    time_str=None,
    name=None,
):
    user = os.getenv("USER")
    if user == "nboehm":  # cin cluster
        if not isinstance(partition, list):
            partitions = [partition] * len(deplist)
        else:
            partitions = partition
        # we have to call stuff for the gpu sequentially
        [
            redo_ifchange(dep)
            for dep, p in zip(deplist, partitions)
            if _is_gpu_partition(p)
        ]
        # rest can be run in parallel
        redo_ifchange(
            [
                dep
                for dep, p in zip(deplist, partitions)
                if not _is_gpu_partition(p)
            ]
        )

    elif user == "jnboehm91":  # mlcloud
        if isinstance(deplist, (str, Path)):
            deplist = [deplist]
        deplist = [Path(dep) for dep in deplist]
        # check whether we're in /mnt/qb/work/... ($WORK)
        workdir = Path(os.getenv("WORK"))
        assert all(
            dep.resolve().is_relative_to(workdir) for dep in deplist
        ), f"All paths must be in {workdir = }"

        is_ood = _redo_ood_list(deplist)
        _slurm_launch_and_wait(
            _redo_filter_list(deplist, is_ood),
            user,
            partition=_redo_filter_list(partition, is_ood),
            n_cpus=_redo_filter_list(n_cpus, is_ood),
            mem=mem,
            time_str=_redo_filter_list(time_str, is_ood),
            names=_redo_filter_list(name, is_ood),
        )

        # Now they should be all done, so we call redo again to have
        # them tracked as a dependecy.  If they failed, then they'll
        # be actually computed (which is likely to fail, should it
        # need a gpu).
        redo_ifchange(deplist)

    else:
        raise ValueError(f"Unknown user “{user}”, not sure what to do.")


def _redo_ood_list(deplist):
    ood_proc = subprocess.run(
        ["redo-ood", *deplist], encoding="utf-8", capture_output=True
    )
    if ood_proc.returncode == 0:
        ood_list = ood_proc.stdout.split("\n")
        return [str(dep) in ood_list for dep in deplist]
    else:
        return [True for _ in deplist]


def _redo_filter_list(list_maybe, bools):
    return (
        list_maybe
        if not isinstance(list_maybe, list)
        else [l for l, b in zip(list_maybe, bools) if b]
    )


def _slurm_launch_and_wait(
    deplist,
    user,
    partition="cpu-short",
    n_cpus=2,
    mem="10G",
    time_str="3-00:00",
    names=None,
):
    if not isinstance(partition, list):
        partition = [partition] * len(deplist)
    if not isinstance(n_cpus, list):
        n_cpus = [n_cpus] * len(deplist)
    if not isinstance(mem, list):
        mem = [mem] * len(deplist)
    if not isinstance(time_str, list):
        time_str = [time_str] * len(deplist)
    if names is None:
        names = [f"redo{i:02d}" for i in range(len(deplist))]
    elif not isinstance(names, list):
        names = [names] * len(deplist)

    procs = []
    job_ids = []
    # launch the parallel jobs
    for name, dep, part, n_cpu, m, t in zip(
        names, deplist, partition, n_cpus, mem, time_str
    ):
        sbatch_args = []
        if part is not None:
            sbatch_args += ["--partition", part]
        if n_cpu is not None:
            sbatch_args += ["--cpus-per-task", str(n_cpu)]
        if m is not None:
            sbatch_args += ["--mem", m]
        if t is not None:
            sbatch_args += ["--time", t]

        if _is_gpu_partition(part):
            sbatch_args += [
                "--gres=gpu:1",
            ]
        # the directory needs to exist so the job is launched and
        # slurm can pollute it with the out and err files.
        Path(dep).parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env.pop("REDO_JS_FD", None)  # REDO_MAKE=none (default)
        env.pop("MAKEFLAGS", None)  # REDO_MAKE=gmake
        # remove file descriptors because they're on another machine
        env.pop("REDO_STATUS_FD", None)
        env.pop("REDO_DEP_FD", None)

        # not quite sure how to launch it, srun, salloc, sbatch?
        # could feed stuff to sbatch via stdin

        inp = "#!/bin/sh\n" f"redo-ifchange '{dep}'\n"
        # print(os.getenv("PWD"), "\t", *[
        #     "sbatch",
        #     *sbatch_args,
        #     "--job-name",
        #     name,
        # ], file=sys.stderr)
        # print(inp, file=sys.stderr)
        proc = subprocess.run(
            [
                "sbatch",
                *sbatch_args,
                "--job-name",
                name,
            ],
            input=inp,
            encoding="utf-8",
            shell=False,
            capture_output=True,
            close_fds=False,
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "sbatch subprocess failed: exit code "
                f"{proc.returncode}\n{proc.stderr}"
            )
        slurm_job_id = re.match(r"Submitted batch job (\d+)", proc.stdout)[1]
        job_ids.append(slurm_job_id)
        procs.append(proc)
        # print(f"{name = }, {slurm_job_id = }",
        #       file=sys.stderr)

    names_str = ",".join(names)
    jobs_str = ",".join(job_ids)
    # poll for exit
    while (
        len(
            subprocess.check_output(
                [
                    "squeue",
                    "--noheader",
                    "--user",
                    user,
                    "--jobs",
                    jobs_str,
                    "--name",
                    names_str,
                    "--states=RUNNING,PENDING,COMPLETING,PREEMPTED",
                ]
            )
        )
        > 0
    ):
        time.sleep(0.5)


def _is_gpu_partition(p):
    return p in [
        "gpu-2080ti",
        "gpu-2080ti-dev",
        "gpu-2080ti-preemptable",
        # "gpu-2080ti-interactive",
        "gpu-v100",
        "gpu-v100-preemptable",
    ]
