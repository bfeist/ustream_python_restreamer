FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY ums_client.py .
COPY ums_grid_splitter.py .

# Create segments directory
RUN mkdir -p segments

# Run the application
CMD ["python", "ums_grid_splitter.py"]
