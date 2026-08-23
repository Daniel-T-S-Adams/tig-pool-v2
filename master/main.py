import logging
import os
import threading
import time
import traceback
from master.data_fetcher import *
from master.job_manager import *
from master.precommit_manager import *
from master.slave_manager import *
from master.submissions_manager import *
from master.client_manager import *

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

# TIG accepts one precommit POST per 5 seconds. Extra creates 503 and then
# 429-block get-block. Pace creates off the slow job_manager loop. Proofs
# and benchmarks use the same 5s-per-endpoint rule — drain them here so
# a long job_manager.run() cannot leave 20 local-proved jobs unsent.
PRECOMMIT_PACE_S = max(5.0, float(os.environ.get("PRECOMMIT_PACE_S", "5")))


def _create_pacer(precommit_manager, submissions_manager):
    while True:
        t0 = time.time()
        try:
            submissions_manager.submit_due_outputs()
            if getattr(precommit_manager, "last_block_id", None):
                created = precommit_manager.run_tick()
                if created:
                    req = created[0]
                    if submissions_manager.submit_precommit(req):
                        precommit_manager.note_precommit_accepted(
                            getattr(getattr(req, "settings", None), "challenge_id", None)
                        )
                    elif getattr(submissions_manager, "last_tig_over_100", False):
                        precommit_manager.note_tig_cap_hit()
        except Exception as exc:
            traceback.print_exc()
            logger.error("%s", exc)
        time.sleep(max(0.05, PRECOMMIT_PACE_S - (time.time() - t0)))


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

    pacer = threading.Thread(
        target=_create_pacer,
        args=(precommit_manager, submissions_manager),
        name="precommit-pacer",
        daemon=True,
    )
    pacer.start()

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
            submissions_manager.run(None)
            slave_manager.run()
        except Exception as e:
            traceback.print_exc()
            logger.error(f"{e}")
        finally:
            hole = bool(
                getattr(precommit_manager, "last_cpu_hole", False)
                or getattr(precommit_manager, "last_gpu_hole", False)
            )
            time.sleep(1 if hole else 5)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(levelname)s - [%(name)s] - %(message)s",
        level=logging.DEBUG if os.environ.get("VERBOSE") else logging.INFO,
    )

    main()
