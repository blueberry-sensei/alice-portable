# Chunking Tokenizer Assets

Put the real tokenizer file at:

- `pipeline/modules/load/chunking/tokenizer/assets/tokenizer.json`

Or point at it with an environment variable:

- `DATAFLOW_CHUNKING_TOKENIZER_JSON`
- `DATAFLOW_TOKENIZER_JSON`

`MarkdownTextChunker` and `MarkdownSourceChunkAssembler` use that tokenizer for exact token counting.
