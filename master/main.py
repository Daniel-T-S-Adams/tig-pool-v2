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

# When the CPU fleet is underfed, submit more than one precommit per 5s tick.
# Default 4 ⇒ up to ~48 creates/min theoretical vs ~12 without burst.
PRECOMMIT_IDLE_BURST = max(1, int(os.environ.get("PRECOMMIT_IDLE_BURST", "4")))


def main():
    last_block_id = None

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
            if data["block"].id != last_block_id:
                last_block_id = data["block"].id
                client_manager.on_new_block(**data)
                job_manager.on_new_block(**data)
                submissions_manager.on_new_block(**data)
                precommit_manager.on_new_block(**data)
            job_manager.run()
            submit_precommit_req = precommit_manager.run()
            submissions_manager.run(submit_precommit_req)
            # Burst creates only while idle CPUs still need work.
            if (
                PRECOMMIT_IDLE_BURST > 1
                and getattr(precommit_manager, "last_idle_cpu_needs_work", False)
            ):
                for _ in range(PRECOMMIT_IDLE_BURST - 1):
                    if not getattr(precommit_manager, "last_idle_cpu_needs_work", False):
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
