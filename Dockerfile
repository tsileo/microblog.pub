FROM python:3.6
ADD . /app
WORKDIR /app
RUN pip install -r requirements.txt
ENV FLASK_APP=app.py
CMD ["gunicorn", "-t", "300", "-w", "2", "-b", "0.0.0.0:5005", "--log-level", "debug", "app:app"]
