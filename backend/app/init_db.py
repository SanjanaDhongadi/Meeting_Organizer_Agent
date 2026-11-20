import logging

logger = logging.getLogger("init_db")

def init_and_seed_db():
    logger.info("Employee profiles are database records and are not seeded at startup.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_and_seed_db()
