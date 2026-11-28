FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py model_api.py railway_start.py points.py social.py match_orchestration.py seat_personas.py question_bank.py ./
COPY static ./static
RUN mkdir -p /app/data
EXPOSE 8000
CMD ["python", "railway_start.py"]
