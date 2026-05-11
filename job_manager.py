import threading
import uuid
import json
from datetime import datetime

jobs = {}
lock = threading.Lock()


def create_job():
    job_id = str(uuid.uuid4())
    with lock:
        jobs[job_id] = {
            "status": "pending",
            "result": None,
            "error": None,
            "created_at": datetime.now().isoformat(),
            "finished_at": None,
        }
    return job_id


def set_running(job_id):
    with lock:
        if job_id in jobs:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = datetime.now().isoformat()


def set_done(job_id, result):
    # Vérifier que result est sérialisable, sinon convertir
    try:
        json.dumps(result)
    except (TypeError, ValueError) as e:
        print(f"[!] set_done: résultat non-sérialisable ({e}), conversion en string")
        result = [{"error": f"résultat non-sérialisable: {e}"}]

    with lock:
        if job_id in jobs:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = result
            jobs[job_id]["finished_at"] = datetime.now().isoformat()
    print(f"[JOB {job_id}] Terminé avec {len(result) if isinstance(result, list) else 1} résultats")


def set_error(job_id, error):
    with lock:
        if job_id in jobs:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(error)
            jobs[job_id]["finished_at"] = datetime.now().isoformat()
    print(f"[JOB {job_id}] Erreur: {error}")


def get_job(job_id):
    return jobs.get(job_id)