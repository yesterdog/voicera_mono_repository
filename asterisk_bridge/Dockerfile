FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./asterisk_bridge

CMD ["python", "-m", "asterisk_bridge.voicera_bridge"]
