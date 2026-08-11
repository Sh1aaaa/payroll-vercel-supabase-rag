import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client

load_dotenv()
root = Path(__file__).resolve().parents[1]
text = (root / "knowledge/payroll_rules.md").read_text(encoding="utf-8")
sections = []
for block in text.split("\n## "):
    block = block.strip()
    if not block:
        continue
    lines = block.splitlines()
    title = lines[0].lstrip("# ").strip()
    content = "\n".join(lines[1:]).strip() or title
    sections.append((title, content))

ai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
for title, content in sections:
    emb = ai.embeddings.create(
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        input=content,
    ).data[0].embedding
    db.table("knowledge_chunks").insert({
        "title": title, "content": content, "source": "knowledge/payroll_rules.md", "embedding": emb
    }).execute()
    print("seeded", title)
