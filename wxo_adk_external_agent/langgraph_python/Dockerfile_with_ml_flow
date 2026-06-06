FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y curl build-essential nginx supervisor && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --upgrade pip

# Clear default Nginx config
RUN rm /etc/nginx/sites-enabled/default

# Copy requirements and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY *.py *.json ./

# Copy configuration files
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Expose port 8080 for Nginx
EXPOSE 8080

# Start supervisord
CMD ["/usr/bin/supervisord"]