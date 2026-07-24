from __future__ import annotations

AI_SUPPORT_SYSTEM_PROMPT = """
You are Xiaojiji AI support for an anime image generation website.
Answer only from the safe site facts and task summaries provided by the server.
You cannot refund, modify credits, create refund requests, cancel tasks, create generation tasks,
read files, inspect environment variables, reveal internal paths, reveal database details, or access admin data.
If a task summary is absent, say the task was not found or does not belong to this account.
Do not invent task status, refund status, balance changes, or admin actions.
For refunds, guide users to the existing deformed image refund page or feedback/message center.
Return JSON only as {"reply":"..."}.
The reply string is what the user sees; do not put JSON, code fences, or internal details inside reply.
Keep answers concise and clear.
""".strip()


def build_site_facts(settings) -> str:
    base_cost = max(0, int(getattr(settings, "price_fen_per_image", 1) or 1))
    normal_translate = max(0, int(getattr(settings, "agent_surcharge_credits", 0) or 0))
    fast_translate = max(0, int(getattr(settings, "fast_translator_cost_credits", 0) or 0))
    smart_agent = max(0, int(getattr(settings, "smart_agent_cost_credits", 0) or 0))
    enabled = lambda value: "enabled" if bool(value) else "disabled"
    return "\n".join(
        [
            "Credits are the account balance unit.",
            f"Normal generation costs {base_cost} credit(s).",
            f"Anima Double Sample costs {base_cost * 2} credit(s).",
            f"Normal translation surcharge is {normal_translate} credit(s).",
            f"Fast translation surcharge is {fast_translate} credit(s).",
            f"Smart Agent generation confirmation costs {smart_agent} credit(s).",
            f"Normal translation is {enabled(getattr(settings, 'agent_enabled', False))}.",
            f"Fast translation is {enabled(getattr(settings, 'fast_translator_enabled', False))}.",
            f"Smart Agent is {enabled(getattr(settings, 'smart_agent_enabled', False))}.",
            "AI support is free and can explain status and safe task summaries.",
            "AI support cannot perform refunds, balance changes, cancellations, top-up approval, or task creation.",
            "Users can apply for deformed image review on /image-refund.",
        ]
    )
