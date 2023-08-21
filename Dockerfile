# Use the official Python image as the base image
FROM python:3.10

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y libpq-dev

# Copy the project files to the container's working directory
COPY requirements.txt .

# Install project dependencies
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Set build args
ARG host
ARG port
ENV HOST=${host}
ENV PORT=${port}

# Expose the port that your FastAPI app runs on
EXPOSE $PORT

CMD ["uvicorn", "app:app", "--reload", "--host", "0.0.0.0", "--port", "8000"]