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
            created = precommit_manager.run_tick()
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
