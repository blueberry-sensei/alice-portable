"""
Text processing utilities
"""

import hashlib
import re
import unicodedata
from typing import List, Optional


def normalize_text(text: str) -> str:
    """
    Normalise text

    Args:
        text: the raw text

    Returns:
        The normalised text
    """
    # Collapse extra whitespace
    text = re.sub(r"\s+", " ", text)
    # Strip leading and trailing whitespace
    text = text.strip()
    return text


def normalize_text_for_embedding(text: str) -> str:
    """
    Normalise text for vector embedding

    Follows industry best practice and suits semantic search, similarity computation and so on.
    It preserves the semantics (punctuation, digits, multilingual characters) and only performs the necessary cleaning.

    Steps:
    1. Unicode normalisation (NFKC) - one canonical character representation
    2. Lowercasing - removes case differences
    3. Whitespace cleanup - normalises whitespace and collapses runs

    Args:
        text: the raw text

    Returns:
        The normalised text

    Examples:
        >>> normalize_text_for_embedding("Hello WORLD!")
        "hello world!"

        >>> normalize_text_for_embedding("  Multiple   spaces  ")
        "multiple spaces"

        >>> normalize_text_for_embedding("OpenAI released the sophnet/Qwen3-30B-A3B-Thinking-2507 model")
        "openai released the sophnet/qwen3-30b-a3b-thinking-2507 model"

        >>> normalize_text_for_embedding("1/2 cup -> 1/2 cup")  # Unicode normalisation
        "1/2 cup -> 1/2 cup"

    Notes:
        - punctuation is kept: it distinguishes meaning (for example "don't" versus "dont")
        - digits are kept: they matter in technical documents (for example "Python 3.11")
        - multilingual characters are kept: mixed-language text still works
        - NFKC normalisation: unifies full-width/half-width, superscript/subscript and similar variants
    """
    if not text:
        return ""

    # 1. Unicode normalisation (NFKC)
    # Converts full-width characters to half-width and unifies compatibility characters
    text = unicodedata.normalize("NFKC", text)

    # 2. Lowercase (multilingual aware)
    text = text.lower()

    # 3. Clean up whitespace
    # Replace every whitespace character (space, tab, newline and so on) with a single space
    text = re.sub(r"\s+", " ", text)

    # 4. Strip leading and trailing whitespace
    text = text.strip()

    return text


def normalize_entity_name(name: str) -> str:
    """
    Normalise an entity name

    Args:
        name: the raw entity name

    Returns:
        The normalised entity name
    """
    # Lowercase
    normalized = name.lower()
    # Strip punctuation (CJK characters are kept)
    normalized = re.sub(r"[^\w\s\u4e00-\u9fff]", "", normalized)
    # Collapse extra whitespace
    normalized = re.sub(r"\s+", " ", normalized)
    # Strip leading and trailing whitespace
    normalized = normalized.strip()
    return normalized


def extract_markdown_headings(content: str) -> List[str]:
    """
    Extract the Markdown headings

    Args:
        content: the Markdown content

    Returns:
        The heading list
    """
    pattern = r"^(#{1,6})\s+(.+)$"
    headings = []

    for line in content.split("\n"):
        match = re.match(pattern, line.strip())
        if match:
            level = len(match.group(1))
            title = match.group(2)
            headings.append(f"{'#' * level} {title}")

    return headings


def compute_text_hash(text: str) -> str:
    """
    Compute the hash of a text

    Args:
        text: the text content

    Returns:
        The MD5 hash (hexadecimal string)
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def truncate_text(
    text: str,
    max_length: int = 100,
    suffix: str = "...",
) -> str:
    """
    Truncate a text

    Args:
        text: the raw text
        max_length: maximum length
        suffix: the suffix

    Returns:
        The truncated text
    """
    if len(text) <= max_length:
        return text

    return text[: max_length - len(suffix)] + suffix


def normalize_heading_text(
    text: Optional[str],
    max_length: int = 500,
    suffix: str = "...",
) -> str:
    """
    Normalise a heading text, truncating it when it is too long.

    Args:
        text: the raw heading text
        max_length: maximum length
        suffix: truncation suffix

    Returns:
        The normalised heading text
    """
    if not text:
        return ""

    normalized = text.strip()
    normalized = re.sub(r"^#{1,6}\s*", "", normalized)
    normalized = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", normalized)
    normalized = " ".join(normalized.split())

    if max_length <= 0 or len(normalized) <= max_length:
        return normalized

    if max_length <= len(suffix):
        return normalized[:max_length]

    return normalized[: max_length - len(suffix)].rstrip() + suffix


def split_text_by_paragraphs(text: str) -> List[str]:
    """
    Split a text into paragraphs

    Args:
        text: the raw text

    Returns:
        The paragraph list
    """
    paragraphs = text.split("\n\n")
    return [p.strip() for p in paragraphs if p.strip()]


def count_chinese_characters(text: str) -> int:
    """
    Count the CJK characters

    Args:
        text: the text content

    Returns:
        The CJK character count
    """
    return len([c for c in text if "\u4e00" <= c <= "\u9fff"])


def estimate_tokens(text: str, method: str = "simple") -> int:
    """
    Estimate the token count of a text

    Args:
        text: the text content
        method: estimation method (simple | tiktoken)

    Returns:
        The estimated token count
    """
    if method == "tiktoken":
        try:
            import tiktoken

            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(text))
        except ImportError:
            pass

    # Simple estimate: CJK 1.5 characters per token, Latin 4 characters per token
    chinese_count = count_chinese_characters(text)
    english_count = len(text) - chinese_count

    return int(chinese_count / 1.5 + english_count / 4)


def clean_whitespace(text: str) -> str:
    """
    Clean the whitespace in a text

    Args:
        text: the raw text

    Returns:
        The cleaned text
    """
    # Collapse runs of spaces into one
    text = re.sub(r" +", " ", text)
    # Collapse runs of newlines into two
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip whitespace at the start and end of each line
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines)


class TokenEstimator:
    """Generic token estimator"""

    def __init__(self, model_type: str = "generic"):
        """
        Initialise the token estimator

        Args:
            model_type: model type ("gpt", "claude", "llama", "generic")
        """
        self.model_type = model_type

    def estimate_tokens(self, text: str) -> int:
        """
        Estimate the token count of a text

        Args:
            text: the text content

        Returns:
        The estimated token count
        """
        if not text:
            return 0

        # Count the CJK characters
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))

        # Count the Latin words
        english_words = len(re.findall(r"\b[a-zA-Z]+\b", text))

        # Adjust the estimate by model type
        if self.model_type == "gpt":
            # GPT models: roughly 1.5 CJK characters per token, roughly 4 Latin characters per token
            chinese_tokens = int(chinese_chars * 0.7)
            english_tokens = english_words
        elif self.model_type == "claude":
            # Claude models: similar to GPT but slightly different
            chinese_tokens = int(chinese_chars * 0.65)
            english_tokens = int(english_words * 1.1)
        elif self.model_type == "llama":
            # LLaMA models: closer to character level
            chinese_tokens = int(chinese_chars * 0.8)
            english_tokens = int(english_words * 1.3)
        else:
            # Generic estimate: a conservative strategy
            chinese_tokens = int(chinese_chars * 0.8)
            english_tokens = english_words

        # Account for punctuation and special characters
        special_chars = len(re.findall(r"[^\w\s\u4e00-\u9fff]", text))
        special_tokens = int(special_chars * 0.5)

        total_tokens = chinese_tokens + english_tokens + special_tokens

        # Return at least 1 (when the text is not empty)
        return max(1, total_tokens)
