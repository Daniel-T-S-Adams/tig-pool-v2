import logging
import os
import time
from master.data_fetcher import *
from master.job_manager import *
from master.precommit_manager import *
from master.slave_manager import *
from master.submissions_manager import *
from master.client_manager import *

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

# When the CPU/GPU fleet is underfed, submit more than one precommit per 5s tick.
# Burst size is min(deficit, PRECOMMIT_IDLE_BURST_MAX, remaining unassigned cap).
PRECOMMIT_IDLE_BURST = max(1, int(os.environ.get("PRECOMMIT_IDLE_BURST", "4")))
PRECOMMIT_IDLE_BURST_MAX = max(
    PRECOMMIT_IDLE_BURST,
    int(os.environ.get("PRECOMMIT_IDLE_BURST_MAX", "16")),
)


def main():
    last_block_id = None
    last_data_generation = None

    client_manager = ClientManager()
    client_manager.start()

    data_fetcher = DataFetcher()
    job_manager = JobManager()
    precommit_manager = PrecommitManager()
    submissions_manager = SubmissionsManager()

    slave_manager = SlaveManager()
    slave_manager.start()

    while True:
        try:
            data = data_fetcher.run()
            generation = data.get("generation")
            if (
                data["block"].id != last_block_id
                or generation != last_data_generation
            ):
                last_block_id = data["block"].id
                last_data_generation = generation
                client_manager.on_new_block(**data)
                job_manager.on_new_block(**data)
                submissions_manager.on_new_block(**data)
                precommit_manager.on_new_block(**data)
            job_manager.run()
            submit_precommit_req = precommit_manager.run()
            submissions_manager.run(submit_precommit_req)
            # Burst creates while idle CPUs/GPUs still have no claimable work.
            extra = max(
                0,
                int(getattr(precommit_manager, "last_idle_burst", 1) or 1) - 1,
            )
            extra = min(extra, PRECOMMIT_IDLE_BURST_MAX)
            if extra > 0 and (
                getattr(precommit_manager, "last_idle_cpu_needs_work", False)
                or getattr(precommit_manager, "last_idle_gpu_needs_work", False)
            ):
                logger.info(
                    "idle create burst extra=%s cpu_need=%s gpu_need=%s",
                    extra,
                    getattr(precommit_manager, "last_idle_cpu_needs_work", False),
                    getattr(precommit_manager, "last_idle_gpu_needs_work", False),
                )
                for _ in range(extra):
                    if not (
                        getattr(precommit_manager, "last_idle_cpu_needs_work", False)
                        or getattr(precommit_manager, "last_idle_gpu_needs_work", False)
                    ):
                        break
                    req = precommit_manager.run()
                    if not req:
                        break
                    submissions_manager.run(req)
            slave_manager.run()
        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error(f"{e}")
        finally:
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(levelname)s - [%(name)s] - %(message)s",
        level=logging.DEBUG if os.environ.get("VERBOSE") else logging.INFO,
    )

    main()
