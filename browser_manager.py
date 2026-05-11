"""
browser_manager.py
──────────────────
Fait tourner Playwright dans UN seul thread dédié.
Flask envoie des tâches via une queue, le thread les exécute et renvoie le résultat.
"""

import threading
import queue
import traceback


class BrowserTask:
    def __init__(self, fn, *args, **kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.result = None
        self.error = None
        self.done = threading.Event()

    def run(self):
        try:
            self.result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            self.error = e
            traceback.print_exc()
        finally:
            self.done.set()


class BrowserThread:
    """
    Thread unique qui possède Playwright du début à la fin.
    Toutes les fonctions scraper sont exécutées ICI, jamais ailleurs.
    """
    def __init__(self):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        """Boucle principale du thread Playwright."""
        print("[BrowserThread] Démarré")
        while True:
            task = self._queue.get()
            if task is None:          # signal d'arrêt
                print("[BrowserThread] Arrêt")
                break
            task.run()

    def run(self, fn, *args, timeout=300, **kwargs):
        """
        Soumet une fonction au thread Playwright et attend le résultat.
        timeout : secondes max d'attente (défaut 5 min)
        """
        task = BrowserTask(fn, *args, **kwargs)
        self._queue.put(task)
        finished = task.done.wait(timeout=timeout)
        if not finished:
            raise TimeoutError(f"La tâche {fn.__name__} a dépassé {timeout}s")
        if task.error:
            raise task.error
        return task.result

    def stop(self):
        self._queue.put(None)
        self._thread.join(timeout=10)


# Instance globale unique
_browser_thread = BrowserThread()


def run_in_browser_thread(fn, *args, timeout=300, **kwargs):
    """Point d'entrée utilisé par app.py et batch.py."""
    return _browser_thread.run(fn, *args, timeout=timeout, **kwargs)


def stop_browser_thread():
    _browser_thread.stop()