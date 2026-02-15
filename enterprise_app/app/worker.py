import time

from .config import settings
from .database import SessionLocal
from .workflow_engine import dispatch_one_job


def run_worker_loop() -> None:
    print("Starting enterprise worker loop...")
    print(f"poll_seconds={settings.worker_poll_seconds} max_jobs_per_cycle={settings.worker_max_jobs_per_cycle}")
    while True:
        dispatched = 0
        db = SessionLocal()
        try:
            for _ in range(settings.worker_max_jobs_per_cycle):
                job = dispatch_one_job(db)
                if not job:
                    break
                dispatched += 1
                print(f"Processed job {job.id} status={job.status}")
        finally:
            db.close()
        if dispatched == 0:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    run_worker_loop()

