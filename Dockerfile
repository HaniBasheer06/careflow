FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py app.js clinic.html clinic.js index.html styles.css waiting-room.html waiting-room.js ./

ENV PORT=3018
EXPOSE 3018

CMD ["gunicorn", "--bind", "0.0.0.0:3018", "app:app"]
