"""Resolve the active persona and relevant owner-scoped Star Map memories."""

from __future__ import annotations


async def load_reply_context(context, *, umo: str, query: str) -> tuple[str, list[str]]:
    """Read persona and bounded relevant memories without paid embedding calls.

    Args:
        context: Running AstrBot context.
        umo: Exact authorized private conversation origin.
        query: The selected post and comment text, used only in memory.

    Returns:
        Active persona prompt and up to three matching Star Map contents.

    Raises:
        RuntimeError: If the selected persona or memory manager is unavailable.
    """
    conversation_id = await context.conversation_manager.get_curr_conversation_id(umo)
    conversation = (
        await context.conversation_manager.get_conversation(umo, conversation_id)
        if conversation_id
        else None
    )
    _, persona, _, _ = await context.persona_manager.resolve_selected_persona(
        umo=umo,
        conversation_persona_id=getattr(conversation, "persona_id", None),
        platform_name="aiocqhttp",
    )
    prompt = persona.get("prompt", "") if isinstance(persona, dict) else ""
    if not isinstance(prompt, str) or not prompt.strip():
        raise RuntimeError("qzone_comment_persona_unavailable")
    metadata = context.get_registered_star("astrbot_plugin_ob_memory")
    manager = getattr(getattr(metadata, "star_cls", None), "manager", None)
    if manager is None:
        raise RuntimeError("qzone_comment_memory_unavailable")
    owner = umo.rsplit(":", 1)[-1]
    from data.plugins.astrbot_plugin_ob_memory.core.search_service import SearchService

    hits = await SearchService(manager, embedding=None).search(
        f"user:{owner}",
        query[:3000],
        limit=3,
        include_archived=False,
    )
    return prompt.strip(), [hit.bucket.content[:800] for hit in hits]
