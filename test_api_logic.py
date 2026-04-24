import os
from api import retrieve_context, build_system_prompt, az_client, CHAT_DEPLOYMENT, detect_safety_level

def test_rag():
    queries = [
        "I've been working 60 hour weeks and I'm just completely burned out. Sometimes I feel like there's no point anymore.",
        "My buddy on site got laid off today, feeling really down about job security."
    ]

    for query in queries:
        print("\n" + "="*80)
        print(f"USER QUERY: {query}")
        print("="*80)
        
        # 1. Check safety
        safety = detect_safety_level(query)
        print(f"Detected Safety Level: {safety}\n")
        
        # 2. Retrieve context
        print("--- RETRIEVING CONTEXT ---")
        ctx = retrieve_context(query)
        print(f"DSM Clinical Context Snippets: {len(ctx['dsm'].split('[Clinical Reference')) - 1 if ctx['dsm'] else 0}")
        if ctx['peer']:
            print(f"Reddit Peer Context Snippets: {len(ctx['peer'].split('[Worker sharing')) + len(ctx['peer'].split('[Peer advice')) + len(ctx['peer'].split('[Peer response')) + len(ctx['peer'].split('[Community discussion')) - 4 if ctx['peer'] else 0}")
            print("\nPreview of injected Reddit Peer Context:")
            print(ctx['peer'][:500] + "...\n")
        else:
            print("No Reddit Peer Context found for this query.\n")

        # 3. Build Prompt & Get Response (Testing at ALGEE Stage 1 - Listen Phase)
        print("--- LLM RESPONSE (Persona: Mack, Stage: Listen) ---")
        prompt = build_system_prompt(
            persona_id="mack", 
            algee_stage=1, 
            dsm_context=ctx['dsm'], 
            peer_context=ctx['peer'], 
            safety_level=safety
        )

        messages = [
            {"role": "system", "content": prompt}, 
            {"role": "user", "content": query}
        ]

        response = az_client.chat.completions.create(
            model=CHAT_DEPLOYMENT,
            messages=messages,
            max_tokens=250,
            temperature=0.7,
        )

        print(response.choices[0].message.content.strip())
        print("\n")

if __name__ == "__main__":
    test_rag()
