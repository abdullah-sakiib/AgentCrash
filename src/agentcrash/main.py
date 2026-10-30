import os

from agentcrash.api import create_app


app = create_app(os.getenv("AGENTCRASH_DATABASE_URL", "sqlite:///./agentcrash.db"))

