"""Accurate token estimator backed by a local tokenizer.json file."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, Optional

from alicecore.utils import get_logger

logger = get_logger("modules.load.chunking.tokenizer")

DEFAULT_TOKENIZER_JSON_PATH = (
    Path(__file__).resolve().parent / "assets" / "tokenizer.json"
)
ENV_KEYS = (
    "DATAFLOW_CHUNKING_TOKENIZER_JSON",
    "DATAFLOW_TOKENIZER_JSON",
)


class TokenizerTokenEstimator:
    """Token estimator using HuggingFace `tokenizers` local JSON model."""

    _tokenizer_cache: Dict[str, object] = {}
    _lock = threading.Lock()

    def __init__(
        self,
        model_type: str = "generic",
        tokenizer_path: Optional[str | Path] = None,
    ) -> None:
        # model_type is kept for signature compatibility with old estimator.
        self.model_type = model_type
        self.tokenizer_path = self._resolve_tokenizer_path(tokenizer_path)
        self._tokenizer = self._load_tokenizer(self.tokenizer_path)

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        try:
            encoded = self._tokenizer.encode(text)
            return len(encoded.ids)
        except Exception as exc:
            raise RuntimeError(f"Tokenizer encode failed: {exc}") from exc

    @classmethod
    def _resolve_tokenizer_path(cls, tokenizer_path: Optional[str | Path]) -> Path:
        if tokenizer_path is not None:
            return Path(tokenizer_path).expanduser().resolve()
        for key in ENV_KEYS:
            value = os.getenv(key)
            if value:
                return Path(value).expanduser().resolve()
        return DEFAULT_TOKENIZER_JSON_PATH

    @classmethod
    def _load_tokenizer(cls, tokenizer_path: Path) -> object:
        key = str(tokenizer_path)
        with cls._lock:
            if key in cls._tokenizer_cache:
                return cls._tokenizer_cache[key]

            if not tokenizer_path.exists():
                raise FileNotFoundError(
                    f"The tokenizer file does not exist: {tokenizer_path}. "
                    f"Put tokenizer.json at that path, or point at it with the "
                    f"{ENV_KEYS[0]}/{ENV_KEYS[1]} environment variable."
                )
            try:
                from tokenizers import Tokenizer
            except ImportError as exc:
                raise ImportError(
                    "The tokenizers dependency is missing. Install `tokenizers` before chunking."
                ) from exc

            try:
                tokenizer = Tokenizer.from_file(str(tokenizer_path))
            except Exception as exc:
                raise RuntimeError(f"Loading the tokenizer failed: {tokenizer_path}, err={exc}") from exc

            cls._tokenizer_cache[key] = tokenizer
            logger.info(f"Tokenizer loaded: {tokenizer_path}")
            return tokenizer
