"""Entry point: python -m assistant"""

from dotenv import load_dotenv

load_dotenv()

from assistant.bot import run

if __name__ == "__main__":
    run()
