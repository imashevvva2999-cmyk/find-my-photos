"""One styling job in its own process, started by the worker (app.worker.style_in_child):

    python -m app.stylejob <photo_id>

Exit code 0 = styled, 1 = failed and recorded. Anything else (killed by the system, crashed) is
recorded by the worker. The photo was already claimed by the worker before this process starts.
"""
import sys

from . import db, worker
from .config import settings
from .observability import setup_logging


def main(photo_id: int) -> int:
    worker.child_setup()
    setup_logging(settings.log_level)
    db.init(max_size=2)
    try:
        with db.connect() as conn:
            photo = conn.execute("SELECT * FROM photos WHERE id = %s", (photo_id,)).fetchone()
        if photo is None:
            return 0   # deleted meanwhile: nothing to do
        return 0 if worker.process_styles(photo) else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1])))
