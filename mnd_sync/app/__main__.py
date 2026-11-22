import logging
import threading

import uvicorn

from .autosync import run_forever
from .settings import DATA_DIR
from .web import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # the HA add-on log
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = create_app(DATA_DIR)
    threading.Thread(target=run_forever, args=(app.state.service, app.state.store, app.state.notifier),
                     daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8099)


if __name__ == "__main__":
    main()
