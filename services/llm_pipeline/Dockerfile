FROM python:3.12.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080

WORKDIR /app

# The one dependency, and only for dense retrieval: rag/dense.py does a 798x1024 dot
# product per query. Everything else here is stdlib, and stays that way -- the module is
# imported lazily in services/llm_pipeline/app.py so that an image without it still serves
# lexical retrieval rather than failing to start.
#
# Not a web framework, not a model: the wheel is ~16 MB and no torch comes with it. The
# corpus vectors themselves are not baked in; they are generated artifacts that change
# with the corpus, so the chart syncs them from S3 the same way vLLM syncs weights.
RUN pip install --no-cache-dir numpy==2.3.4

COPY guardrails/ guardrails/
COPY prompt/ prompt/
COPY rag/ rag/
COPY services/ services/
COPY data/xanhsm_retrieval_mock/corpus/retrieval_corpus.jsonl \
     data/xanhsm_retrieval_mock/corpus/retrieval_corpus.jsonl
# The dense vectors, when they have been built. The directory is tracked (via a .gitkeep)
# and its contents are not, so this copies the index if `make dense-build` has run and
# copies nothing if it has not -- in which case app.py reports dense off at startup and
# serves lexical retrieval. 3.2 MB for 798 chunks; revisit if the corpus grows an order
# of magnitude, at which point it wants the S3-sync treatment vLLM weights get.
COPY data/index/ data/index/

USER 65532:65532
EXPOSE 8080

CMD ["python", "-m", "services.llm_pipeline.app"]
