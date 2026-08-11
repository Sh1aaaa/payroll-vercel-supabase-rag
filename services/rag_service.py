import os
from huggingface_hub import InferenceClient

HF_TOKEN = os.getenv("HF_TOKEN")

client = InferenceClient(
    token=HF_TOKEN
)


def create_embedding(text):
    try:
        result = client.feature_extraction(
            text,
            model="sentence-transformers/all-MiniLM-L6-v2"
        )

        # Convert possible nested output into a plain list
        if hasattr(result, "tolist"):
            result = result.tolist()

        if isinstance(result, list) and len(result) > 0:
            if isinstance(result[0], list):
                result = result[0]

        return result

    except Exception as e:
        print("Embedding error:", str(e))
        raise


def retrieve_relevant_chunks(supabase, complaint_text, limit=5):
    embedding = create_embedding(complaint_text)

    response = supabase.rpc(
        "match_knowledge_chunks",
        {
            "query_embedding": embedding,
            "match_count": limit
        }
    ).execute()

    return response.data or []


def assess_complaint(supabase, complaint_text):
    try:
        chunks = retrieve_relevant_chunks(
            supabase,
            complaint_text
        )

        if chunks:
            context = "\n\n".join(
                [
                    f"Rule {index + 1}: {item.get('content', '')}"
                    for index, item in enumerate(chunks)
                ]
            )
        else:
            context = "No relevant payroll rule was retrieved."

        prompt = f"""
You are an assistant for a payroll complaint assessment system.

Assess the employee complaint using ONLY the payroll and DTR rules
provided in the context.

PAYROLL/DTR RULES:
{context}

EMPLOYEE COMPLAINT:
{complaint_text}

Return a concise assessment containing:

1. Complaint category
2. Relevant payroll/DTR rule
3. Whether the complaint appears valid, invalid, or requires HR review
4. Explanation
5. Recommended HR action

Important:
Do not make the final payroll decision.
The final decision belongs to HR or the Super Admin.
"""

        response = client.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You assess payroll complaints using retrieved "
                        "payroll and DTR policies."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=500
        )

        return response.choices[0].message.content

    except Exception as e:
        print("RAG assessment error:", str(e))

        return (
            "AI assessment could not be completed. "
            "The complaint has been forwarded for HR review."
        )
