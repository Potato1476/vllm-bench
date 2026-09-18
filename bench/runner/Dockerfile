# The measurement engine is vLLM's own `vllm bench serve`, so the runner is built on the
# image that ships it rather than re-implementing a load generator in a slim image.
#
# The cost is real: this image is large, and the tooling node pulls it once per session.
# It buys a client that is battle-tested under load and produces numbers directly
# comparable to published vLLM benchmarks, which a hand-rolled asyncio client does not.
FROM vllm/vllm-openai:v0.29.0

# boto3 only; everything else the runner needs is already in the base image.
RUN pip install --no-cache-dir boto3==1.40.0

WORKDIR /app
COPY run_bench.py /app/run_bench.py

# Stamped at build time so a result file can name the commit that produced it even though
# there is no git checkout in the image.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

ENTRYPOINT ["python3", "/app/run_bench.py"]
