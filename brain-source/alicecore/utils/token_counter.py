"""
Token consumption statistics utility

Tracks the token consumption of every LLM call. It supports:
- recording the input/output/total tokens of each LLM call automatically
- statistics grouped by scenario (NER, rerank, query rewrite and so on)
- saving the log into the output folder
- both a decorator and a context manager

Usage examples:
    # Form 1: decorator
    @track_tokens(scenario="ner")
    async def extract_entities(query: str):
        response = await llm_client.chat_with_schema(...)
        return response

    # Form 2: context manager
    async with TokenTracker(scenario="rerank") as tracker:
        response = await llm_client.chat_with_schema(...)
        tracker.record(response)

    # Form 3: record manually
    tracker = TokenCounter()
    tracker.add_record(
        scenario="ner",
        model="[REDACTED]",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150
    )
    tracker.save_to_file("output/tokens.json")
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from functools import wraps

from alicecore.utils import get_logger

logger = get_logger("utils.token_counter")


class TokenCounter:
    """Token consumption counter"""

    def __init__(self):
        self.records: List[Dict[str, Any]] = []
        self.summary: Dict[str, Dict[str, int]] = {}

    def add_record(
        self,
        scenario: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Add one token consumption record

        Args:
            scenario: scenario name (such as "ner", "rerank", "query_rewrite")
            model: model name (such as "[REDACTED]")
            input_tokens: input token count
            output_tokens: output token count
            total_tokens: total token count
            metadata: extra metadata (such as query, response)
        """
        record = {
            "timestamp": datetime.now().isoformat(),
            "scenario": scenario,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "metadata": metadata or {},
        }
        self.records.append(record)

        # Update the aggregate statistics
        if scenario not in self.summary:
            self.summary[scenario] = {
                "count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
        self.summary[scenario]["count"] += 1
        self.summary[scenario]["input_tokens"] += input_tokens
        self.summary[scenario]["output_tokens"] += output_tokens
        self.summary[scenario]["total_tokens"] += total_tokens

        logger.debug(
            f"[Token record] {scenario} | model={model} | "
            f"input={input_tokens}, output={output_tokens}, total={total_tokens}"
        )

    def get_summary(self) -> Dict[str, Any]:
        """
        Get the aggregate statistics

        Returns:
            {
                "total": {"input_tokens": int, "output_tokens": int, "total_tokens": int},
                "by_scenario": {scenario: {...}, ...}
            }
        """
        total_input = sum(s["input_tokens"] for s in self.summary.values())
        total_output = sum(s["output_tokens"] for s in self.summary.values())
        total_tokens = sum(s["total_tokens"] for s in self.summary.values())

        return {
            "total": {
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_tokens,
            },
            "by_scenario": self.summary,
        }

    def save_to_file(self, output_path: str):
        """
        Save the token log to a file

        Args:
            output_path: output file path (JSON format)
        """
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "summary": self.get_summary(),
            "records": self.records,
        }

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info(f"Token log saved: {output_file}")

    def reset(self):
        """Reset the statistics"""
        self.records.clear()
        self.summary.clear()


# Global singleton
_global_counter = TokenCounter()


def get_global_counter() -> TokenCounter:
    """Get the global token counter"""
    return _global_counter


def reset_global_counter():
    """Reset the global token counter"""
    _global_counter.reset()


class TokenTracker:
    """
    Token tracking context manager

    Usage example:
        async with TokenTracker(scenario="ner") as tracker:
            response = await llm_client.chat_with_schema(...)
            tracker.record(response)
    """

    def __init__(
        self,
        scenario: str,
        counter: Optional[TokenCounter] = None,
    ):
        self.scenario = scenario
        self.counter = counter or _global_counter
        self.start_time = None

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            logger.warning(f"[token tracking] {self.scenario} raised: {exc_val}")
        return False

    async def __aenter__(self):
        self.start_time = time.perf_counter()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            logger.warning(f"[token tracking] {self.scenario} raised: {exc_val}")
        return False

    def record(
        self,
        response: Dict[str, Any],
        model: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        Record the token consumption of an LLM response

        Args:
            response: the LLM response (it must carry a usage field)
            model: the model name
            metadata: extra metadata
        """
        usage = response.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        total_tokens = input_tokens + output_tokens

        elapsed = time.perf_counter() - self.start_time if self.start_time else 0
        meta = metadata or {}
        meta["elapsed_seconds"] = round(elapsed, 3)

        self.counter.add_record(
            scenario=self.scenario,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            metadata=meta,
        )


def track_tokens(scenario: str, counter: Optional[TokenCounter] = None):
    """
    Token tracking decorator

    Usage example:
        @track_tokens(scenario="ner")
        async def extract_entities(query: str):
            response = await llm_client.chat_with_schema(...)
            return response
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with TokenTracker(scenario=scenario, counter=counter) as tracker:
                result = await func(*args, **kwargs)
                # Assume the return value is an LLM response, or a dictionary holding one
                if isinstance(result, dict) and "usage" in result:
                    tracker.record(result)
                return result
        return wrapper
    return decorator


__all__ = [
    "TokenCounter",
    "TokenTracker",
    "track_tokens",
    "get_global_counter",
    "reset_global_counter",
]
