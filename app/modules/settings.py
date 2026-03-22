import os
import dotenv

dotenv.load_dotenv()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
CLOUDFLARE = os.getenv("CLOUDFLARE", "true").strip().lower() == "true"
