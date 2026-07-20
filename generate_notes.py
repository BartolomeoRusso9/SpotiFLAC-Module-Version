import os
import subprocess
from google import genai

# 1. Client Gemini (nuovo SDK unificato)
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# 2. Recupera i commit dall'ultimo tag
log_cmd = "git log $(git describe --tags --abbrev=0)..HEAD --pretty=format:'- %s'"
try:
    commits = subprocess.check_output(log_cmd, shell=True).decode("utf-8")
except Exception:
    commits = subprocess.check_output(
        "git log -10 --pretty=format:'- %s'", shell=True
    ).decode("utf-8")

if not commits.strip():
    commits = "- Maintenance and minor updates"

# 3. Prompt
prompt = f"""
You are a technical assistant. Here are the recent commits of a software repository:
{commits}

Generate professional release notes in Markdown.
Organize them into these sections: New Features, Bug Fixes, and Maintenance.
Explain the changes in simple terms, ignoring system commits like "merge" or "bump version".
"""

# 4. Genera la risposta con un modello del free tier
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt,
)
changelog = response.text

# 5. Output
print("=== NEW RELEASE NOTES ===")
print(changelog)
