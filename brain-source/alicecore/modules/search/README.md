# Search module

## Directory layout

```
search/
├── __init__.py          # exported interface
├── config.py            # configuration
├── searcher.py          # the unified search entry point (SAGSearcher)
├── vector.py            # the VECTOR strategy: pure vector search
├── atomic.py            # the ATOMIC strategy: atomic triple event retrieval
├── multi.py             # the MULTI / MULTI1 / HOPLLM strategies (selected by the strategy parameter)
├── multi_vector.py      # the MULTI_ES strategy: the ES-first version of multi.py
└── step5_strategies.py  # the Step5 multi-hop expansion strategies (Multi / Multi1 / HopLLM)
```

> Note: `MULTI1` / `HOPLLM` are not separate files but strategy branches selected inside `multi.py` through
> `MultiConfig(strategy=...)`; their expansion differences live in `step5_strategies.py`.


## Search strategies

| Strategy | Description |
|------|------|
| `VECTOR` | pure vector retrieval of sections, skipping entity recall; only the PARAGRAPH return type |
| `ATOMIC` | atomic triple event retrieval + LLM selection |
| `MULTI` | multi-element event retrieval (multi-entity + fixed-hop expansion + LLM selection) |
| `MULTI1` | two-stage expansion: one fixed hop for eventset + dynamic hops for eventset1, merged into one LLM selection |
| `HOPLLM` | two-stage hops: stage A coarse-ranks, then stage B expands from those results as seeds |
| `MULTI_ES` | the ES-first version of `MULTI` (`multi_vector.py`), replacing part of the MySQL JOINs with Elasticsearch |

## Usage example

```python
from alicecore.modules.search import SAGSearcher, SearchConfig, RerankStrategy
from alicecore.core.prompt.manager import PromptManager

searcher = SAGSearcher(prompt_manager=PromptManager())

config = SearchConfig(
    query="the latest advances in artificial intelligence",
    source_config_ids=["source_123"],
)

result = await searcher.search(config)
print(f"found {len(result['sections'])} sections")
```

## Result shape

```python
{
    "sections": [...],   # the section list
    "clues": [...],      # the clue list
    "stats": {...},      # the statistics (timings included)
    "query": {
        "original": "...",
        "current": "...",
        "rewritten": False
    }
}
```
