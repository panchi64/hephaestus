import logging

logging.basicConfig(filename="logs/app.log", level=logging.DEBUG)
log = logging.getLogger("app")


def handle(request_id):
    log.debug("handling %s", request_id)
    log.info("done %s", request_id)
