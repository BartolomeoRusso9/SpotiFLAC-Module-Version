import os
import subprocess
import google.generativeai as genai

# 1. AI API Configuration (e.g., Gemini)
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel('gemini-1.5-flash')

# 2. Get the commit logs of the last PR (or the git diff)
# This command extracts the recently added commit messages
log_cmd = "git log $(git describe --tags --abbrev=0)..HEAD --pretty=format:'- %s'"
try:
    commits = subprocess.check_output(log_cmd, shell=True).decode('utf-8')
except Exception:
    commits = subprocess.check_output("git log -10 --pretty=format:'- %s'", shell=True).decode('utf-8')

# 3. Build the Prompt
prompt = f"""
You are a technical assistant. Here are the recent commits of a software repository:
{commits}

Generate professional release notes in Markdown. 
Organize them into these sections: New Features, Bug Fixes, and Maintenance.
Explain the changes in simple terms, ignoring system commits like "merge" or "bump version".
"""

# 4. Generate the response
response = model.generate_content(prompt)
changelog = response.text

# 5. Output (here you can use the GitHub API to create the actual release)
print("=== NEW RELEASE NOTES ===")
print(changelog)