FROM python:3.12.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080

WORKDIR /app

COPY guardrails/ guardrails/
COPY prompt/ prompt/
COPY rag/ rag/
COPY guardrail_service/ guardrail_service/
COPY data/xanhsm_retrieval_mock/corpus/retrieval_corpus.jsonl \
     data/xanhsm_retrieval_mock/corpus/retrieval_corpus.jsonl

USER 65532:65532
EXPOSE 8080

CMD ["python", "-m", "guardrail_service.app"]
