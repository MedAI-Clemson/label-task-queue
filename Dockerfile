FROM python:3.12-slim

# The database and the ontology folder are meant to be mounted (see docker-compose.yml);
# the defaults below let the container start on its own with the example ontologies.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URI=sqlite:////db/ltq.db \
    LTQ_LABEL_TASKS=/ontologies

WORKDIR /code

# alembic.ini and the migrations (src/ltq/store/migrations) stay in /code, where `alembic`
# looks for them; the package itself is installed from src/.
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY examples/ontologies /ontologies
RUN mkdir -p /db

EXPOSE 8000

# Bring the database up to date, then serve. To make the first administrator:
#   docker compose run --rm app sh -c \
#     "alembic upgrade head && python -m ltq.api.admin create-admin <username>"
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn ltq.api:app --host 0.0.0.0 --port 8000"]
