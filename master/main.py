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
            sized = int(getattr(precommit_manager, "last_sized_burst", 0) or 0)
            if sized <= 0:
                sized = int(getattr(precommit_manager, "last_idle_burst", 1) or 1)
            created = [submit_precommit_req] if submit_precommit_req is not None else []
            extra = extra_creates_this_tick(
                sized_burst=sized,
                first_ok=submit_precommit_req is not None,
                max_burst=max(PRECOMMIT_IDLE_BURST_MAX, sized),
            )
            if extra > 0:
                logger.info(
                    "idle create burst extra=%s sized=%s first_ok=%s cpu_need=%s gpu_need=%s",
                    extra,
                    sized,
                    submit_precommit_req is not None,
                    getattr(precommit_manager, "last_idle_cpu_needs_work", False),
                    getattr(precommit_manager, "last_idle_gpu_needs_work", False),
                )
                misses = 0
                for _ in range(extra):
                    req = precommit_manager.run()
                    if not req:
                        misses += 1
                        if misses >= 8:
                            break
                        continue
                    misses = 0
                    created.append(req)
            if created:
                for req in created:
                    submissions_manager.run(req)
            else:
                submissions_manager.run(None)
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
