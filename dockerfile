# Use official Python image
FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Copy bot files
COPY yt-boy.py detect_links.py requirements.txt /app/

# Install node.js
RUN apt-get update && apt-get install -y \
    ffmpeg \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create folder for downloads
RUN mkdir -p /app/downloads

# Set environment variable for your bot token (you can override later)
ENV DISCORD_API_TOKEN=${DISCORD_API_TOKEN}
ENV DB_URL=${DB_URL}
ENV TARGET_CHANNEL_IDS=${TARGET_CHANNEL_IDS}
ENV DOWNLOAD_DIR="/app/downloads"

# Start the bot
CMD ["python", "-u", "yt-boy.py"]