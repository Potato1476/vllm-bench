FROM python:3.12.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8080

WORKDIR /app

# Ban va bao mat cua base image. IMG-002 cua bo quy tac Quality Gate chan moi CVE
# CRITICAL/HIGH **co ban va**, va python:3.12.11-slim (Debian 13.1) den voi 8 cai nhu vay
# trong perl-base va util-linux. Co ban va thi cach dung la lay ban va, khong phai xin
# ngoai le -- mot ngoai le o day chi la ghi lai rang minh chon khong vá.
#
# `upgrade` chu khong phai ghim tung goi: danh sach CVE doi theo tuan, nen ghim phien ban
# se bien moi lan Debian phat hanh ban va thanh mot lan sua Dockerfile.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Two small serving dependencies: numpy computes dense-retrieval/cache similarity and
# redis talks to the pod-local response-cache sidecar. No web framework or model runtime is
# pulled into this image; the dense module is still imported lazily so lexical-only startup
# does not depend on an embedding model.
#
# Neither is a web framework or a model; no torch comes with them. The
# corpus vectors themselves are not baked in; they are generated artifacts that change
# with the corpus, so the chart syncs them from S3 the same way vLLM syncs weights.
RUN pip install --no-cache-dir numpy==2.3.4 redis==6.4.0

# Build the reproducible mock warehouse from tracked sources. The large SQLite/CSV
# outputs are gitignored and absent in clean checkouts; only SQLite is kept in the image.
COPY data/xanhsm_mock_warehouse/scripts/generate_warehouse.py \
     data/xanhsm_mock_warehouse/scripts/generate_warehouse.py
COPY data/xanhsm_mock_warehouse/sql/ data/xanhsm_mock_warehouse/sql/
RUN python data/xanhsm_mock_warehouse/scripts/generate_warehouse.py \
    && rm -rf data/xanhsm_mock_warehouse/csv
# Keep application code after the expensive database layer so code-only fixes reuse it.
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
