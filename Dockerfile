FROM python:3.7
COPY requirements.txt /app/requirements.txt
WORKDIR /app
RUN pip install -r requirements.txt
ADD . /app
ENV FLASK_APP=app.py
CMD ["./run.sh"]
