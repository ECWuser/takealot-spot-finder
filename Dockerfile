FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Avoid Python buffering
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python deps
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . /app

# Streamlit will listen on $PORT provided by Render; default to 10000 locally
EXPOSE 10000
CMD ["/bin/sh","-c","streamlit run app.py --server.port ${PORT:-10000} --server.address 0.0.0.0"]
