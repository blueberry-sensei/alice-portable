"""
Rerank model ordering service

Xếp hạng lại event/đoạn văn qua bất kỳ Rerank API tương thích nào.
"""

from typing import List, Optional, Dict, Any

from alicecore.core.config import get_settings
from alicecore.exceptions import AIError
from alicecore.utils import get_logger
import aiohttp

logger = get_logger("ai.rerank")


def _get_rerank_template_name(server_type: str, model: str):
    """Decide from server_type and the model name whether a rerank template is needed, returning its name; None when no template is needed"""
    if server_type != "LOCAL":
        return None
    model_lower = model.lower()
    if "qwen3" in model_lower and "rerank" in model_lower:
        return "qwen3_rerank"
    return None


def _load_rerank_template(template_name: str):
    """Load the named rerank template from PromptManager and return its fields; None when loading fails"""
    try:
        from alicecore.core.prompt.manager import get_prompt_manager
        pm = get_prompt_manager()
        config = pm.get_template_config(template_name)
        return {
            "prefix": config.get("prefix", ""),
            "suffix": config.get("suffix", ""),
            "query_template": config.get("query_template", ""),
            "doc_template": config.get("doc_template", ""),
            "default_instruction": config.get("default_instruction", ""),
        }
    except Exception as e:
        logger.warning(f"Failed to load the rerank template '{template_name}', skipping prompt assembly: {e}")
        return None


def _format_with_template(query, documents, instruction, tpl):
    """Assemble query and documents with the Qwen3-Rerank template, returning (formatted_query, formatted_docs)"""
    formatted_query = tpl["query_template"].format(
        prefix=tpl["prefix"], instruction=instruction, query=query
    )
    formatted_docs = [
        tpl["doc_template"].format(doc=doc, suffix=tpl["suffix"]) for doc in documents
    ]
    return formatted_query, formatted_docs


class RerankClient:
    """
    Rerank client

    Hoạt động với mọi Rerank API tương thích.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None
    ):
        """
        Initialise the rerank client

        Args:
            model: model name (read from configuration by default)
            base_url: API address (read from configuration by default)
            api_key: API key (read from configuration by default)
        """
        settings = get_settings()

        # Rerank-specific configuration first, falling back to the embedding configuration
        self.model = model or getattr(settings, 'rerank_model_name', None) or "Qwen/Qwen3-Reranker-8B"
        # Base URL để ghép endpoint rerank.
        # KHÔNG có fallback ngầm ra dịch vụ bên ngoài: bản upstream rơi thẳng về
        # endpoint của một gateway bên thứ ba khi thiếu cấu hình, tức là lặng lẽ
        # gửi truy vấn của người dùng ra ngoài. Ở đây thiếu cấu hình thì BÁO LỖI.
        base = (base_url or
                getattr(settings, 'rerank_base_url', None) or
                settings.embedding_base_url)
        if not base:
            raise ValueError(
                "Rerank chưa được cấu hình: cần đặt rerank_base_url (hoặc "
                "embedding_base_url). Không có giá trị mặc định — để tránh gửi "
                "dữ liệu ra một dịch vụ ngoài mà bạn không chủ động chọn."
            )
        self.base_url = base.rstrip('/')
        # Rerank request endpoint path (configurable, default /rerank; also works with /reranks and other routes)
        endpoint = getattr(settings, 'rerank_endpoint', None) or "/rerank"
        endpoint = endpoint.strip()
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        self.endpoint = endpoint
        self.api_key = (api_key or
                       getattr(settings, 'rerank_api_key', None) or
                       settings.embedding_api_key or
                       settings.llm_api_key)

        logger.info(
            f"Rerank client initialised",
            extra={
                "model": self.model,
                "base_url": self.base_url,
            },
        )

    async def rerank(
        self,
        query: str,
        documents: List[Dict[str, str]],
        top_n: Optional[int] = None,
        return_documents: Optional[bool] = False,
        use_prompt_template: bool = False,
        instruction: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Reorder the documents with the rerank model

        Args:
            query: query text
            documents: document list, shaped [{"id": "...", "text": "..."}, ...]
            top_n: return the top N results
            return_documents: whether the response includes the documents (passed to the API, does not change the client return shape)
            use_prompt_template: whether to inject the Qwen3-Rerank prompt template (needed for a LOCAL deployment)
            instruction: rerank instruction; only applies when use_prompt_template=True, defaulting to the template's default_instruction

        Returns:
            The ordered result list, shaped:
            [
                {
                    "index": 0,           # index in the original documents
                    "id": "doc_id",       # document ID
                    "score": 0.95         # relevance score
                },
                ...
            ]
            Note: text is not returned; the caller looks it up in the original documents by id
        """
        if not documents:
            logger.warning("Rerank call skipped: the document list is empty")
            return []

        top_n = top_n or len(documents)
        logger.warning(f"Rerank returning the top-{top_n} ordered events/sections")
        # Extract the document texts
        texts = [doc.get("text", "") for doc in documents]

        # Assemble query and documents with the rerank model's own template when needed
        if use_prompt_template:
            settings = get_settings()
            tpl_name = _get_rerank_template_name(settings.server_type, self.model)
            if tpl_name:
                tpl = _load_rerank_template(tpl_name)
                if tpl:
                    inst = instruction or tpl["default_instruction"]
                    query, texts = _format_with_template(query, texts, inst, tpl)
                    logger.info(f"Injected the rerank prompt template: {tpl_name}")
                else:
                    logger.warning(f"use_prompt_template=True but the template '{tpl_name}' failed to load, using the raw text")
            else:
                logger.info(f"Model '{self.model}' has no dedicated template (server_type={settings.server_type}), using the raw text")

        # Build the rerank request URL:
        # - when base_url already contains the rerank endpoint path (such as /rerank or /reranks), use it as is to avoid doubling
        # - otherwise append the configured self.endpoint (default /rerank, customisable through RERANK_ENDPOINT)
        base_lower = self.base_url.lower()
        if (base_lower.endswith("/rerank") or base_lower.endswith("/reranks")
                or base_lower.endswith(self.endpoint.lower())):
            url = self.base_url
        else:
            url = f"{self.base_url}{self.endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "query": query,
            "top_n": top_n,
            "return_documents": return_documents,  # skip the document content to save bandwidth
            "documents": texts
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise AIError(f"The rerank API returned an error {resp.status}: {error_text}")

                    resp_json = await resp.json()

            # Parse the response (text is not returned; the caller looks it up in the original docs by id)
            results = []
            for item in resp_json.get("results", []):
                idx = item.get("index")
                results.append({
                    "index": idx,
                    "id": documents[idx].get("id", str(idx)),
                    "score": item.get("relevance_score", item.get("score", 0.0))
                })

            logger.info(f"Rerank finished: query='{query[:30]}...', documents={len(documents)}, returned={len(results)}")
            return results

        except Exception as e:
            logger.error(f"The rerank API call failed: {e}")
            raise


# ==================== Global singleton ====================

# Global singleton
_rerank_client: Optional[RerankClient] = None


def get_rerank_client() -> RerankClient:
    """
    Get the global rerank client (singleton)

    Returns:
        A RerankClient instance
    """
    global _rerank_client
    if _rerank_client is None:
        _rerank_client = RerankClient()
    return _rerank_client


def reset_rerank_client() -> None:
    """Reset the global rerank client"""
    global _rerank_client
    _rerank_client = None


async def rerank_documents(
    query: str,
    documents: List[Dict[str, str]],
    top_n: Optional[int] = None,
    use_prompt_template: bool = False,
    instruction: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Convenience function for reranking documents

    Args:
        query: query text
        documents: document list
        top_n: return the top N results
        use_prompt_template: whether to inject the Qwen3-Rerank prompt template
        instruction: rerank instruction

    Returns:
        The ordered result list
    """
    client = get_rerank_client()
    return await client.rerank(query, documents, top_n,
                               use_prompt_template=use_prompt_template,
                               instruction=instruction)
