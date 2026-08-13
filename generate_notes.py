import os
import subprocess

from google import genai

# 1. Client Gemini (nuovo SDK unificato)
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# 2. Recupera i commit dall'ultimo tag
# Usa HEAD^ come fallback nel describe per evitare log vuoti se il tag coincide con l'HEAD attuale
log_cmd = "git log $(git describe --tags --abbrev=0 HEAD^ 2>/dev/null || git rev-list --max-parents=0 HEAD)..HEAD --pretty=format:'- %s'"
try:
    commits = subprocess.check_output(log_cmd, shell=True).decode("utf-8").strip()
except Exception:
    commits = (
        subprocess.check_output(
            "git log -10 --pretty=format:'- %s'",
            shell=True,
        )
        .decode("utf-8")
        .strip()
    )

if not commits:
    commits = "- Maintenance and minor updates"

# 3. Prompt (versione tecnica avanzata e senza emoji)
prompt = f"""You are an expert software engineer and technical writer. Review the following commit logs:
{commits}

Generate highly technical, professional release notes in Markdown format. Organize the changelog strictly into the following sections: **New Features**, **Bug Fixes**, and **Maintenance**.

**Guidelines for technical depth:**
*   **Use specific technical terminology:** Describe changes at the code, architecture, or system level (e.g., specify API endpoints modified, database schema migrations, specific design patterns used, or algorithmic optimizations).
*   **Do not simplify:** Avoid abstracting the changes into layman's terms or user-centric language. The target audience is senior developers and DevOps engineers. 
*   **Be precise:** Mention the exact libraries, variables, or system components affected if present in the commit messages.
*   **Filter noise:** Ignore automated system commits, merge commits, version bumps, and trivial typo fixes. 
*   **Tone:** Strictly professional and objective. DO NOT use emojis.
"""

# 4. Generate the response with a free-tier model
print("Generazione del changelog in corso con gemini-2.5-pro...")
try:
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=prompt,
    )
    changelog = response.text
except Exception as e:
    print(f"Errore durante la generazione API: {e}")
    changelog = "## Changelog\n\n- Maintenance and minor updates."

# 5. Output
with open("ai_changelog.md", "w", encoding="utf-8") as f:
    f.write(changelog)

print("\nChangelog generato con successo e salvato in 'ai_changelog.md':\n")
print("-" * 40)
print(changelog)
print("-" * 40)
