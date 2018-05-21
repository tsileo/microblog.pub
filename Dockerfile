FROM python:3.6
ADD . /app
WORKDIR /app
RUN pip install -r requirements.txt
ENV FLASK_APP=app.py
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5005", "app:app"]
