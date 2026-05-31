import os
from dotenv import load_dotenv

print("Caricamento .env...")
load_dotenv()

api_locale = os.environ.get("QOBUZ_LOCAL_API_URL")
print(f"URL API Letto: {api_locale}")